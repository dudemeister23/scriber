"""File transcription window (AppKit / PyObjC).

Offline pipeline: file picker -> diarize + transcribe -> show result
with Copy / Save as TXT / Save as SRT.
"""

import logging
import os
import threading

import objc
from AppKit import (
    NSAlert,
    NSAlertFirstButtonReturn,
    NSAlertStyleInformational,
    NSApp,
    NSBackingStoreBuffered,
    NSBezelStyleRounded,
    NSButton,
    NSColor,
    NSFont,
    NSOpenPanel,
    NSPopUpButton,
    NSProgressIndicator,
    NSProgressIndicatorStyleBar,
    NSSavePanel,
    NSScrollView,
    NSSegmentedControl,
    NSTextField,
    NSTextView,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskResizable,
    NSWindowStyleMaskTitled,
    NSPasteboard,
    NSPasteboardTypeString,
)
from Foundation import NSMakeRect, NSMakeSize, NSObject

from . import deps_manager
from . import file_transcribe as ft

logger = logging.getLogger("scriber")

WINDOW_WIDTH = 640
WINDOW_HEIGHT = 620
MARGIN = 20


class _ActionTarget(NSObject):
    """Generic ObjC target that routes to a Python callable."""

    def initWithCallback_(self, callback):
        self = objc.super(_ActionTarget, self).init()
        if self is None:
            return None
        self._callback = callback
        return self

    def trigger_(self, sender):
        try:
            self._callback(sender)
        except Exception as e:
            logger.error("Action callback failed: %s", e)


class _MainThreadCall(NSObject):
    """Lets background threads invoke Python callables on the main thread."""

    def initWithCallable_(self, callable_):
        self = objc.super(_MainThreadCall, self).init()
        if self is None:
            return None
        self._callable = callable_
        return self

    def invoke_(self, _sender):
        try:
            self._callable()
        except Exception as e:
            logger.error("Main-thread invoke failed: %s", e)


class FileTranscribeWindowController:
    """Controls the file transcription window and pipeline."""

    def __init__(self, config: dict, save_config_callback):
        self._config = config
        self._save_config_callback = save_config_callback

        self._window = None
        self._file_field = None
        self._browse_btn = None
        self._speakers_popup = None
        self._progress = None
        self._status_label = None
        self._results_view = None
        self._start_btn = None
        self._cancel_btn = None
        self._copy_btn = None
        self._save_txt_btn = None
        self._save_srt_btn = None
        self._preview_segmented = None
        self._preview_format = "txt"  # "txt" or "srt"

        self._selected_file = ""
        self._segments = []  # list[TranscribedSegment] after completion
        self._cancel_event = None
        self._worker_thread = None

        # Keep targets alive
        self._targets = []
        self._main_thread_dispatchers = []

        self._build_window()

    # --- Window building ---

    def _build_window(self):
        rect = NSMakeRect(0, 0, WINDOW_WIDTH, WINDOW_HEIGHT)
        style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
                 | NSWindowStyleMaskResizable)
        self._window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, style, NSBackingStoreBuffered, False
        )
        self._window.setTitle_("Transcribe File")
        self._window.center()
        self._window.setReleasedWhenClosed_(False)
        self._window.setMinSize_(NSMakeSize(480, 480))

        content = self._window.contentView()
        field_width = WINDOW_WIDTH - 2 * MARGIN
        y = WINDOW_HEIGHT - 40

        label_font = NSFont.systemFontOfSize_(11.0)
        label_color = NSColor.secondaryLabelColor()

        # --- Audio file row ---
        y = self._add_label(content, "Audio File",
                            MARGIN, y, field_width, label_font, label_color)

        self._file_field = NSTextField.alloc().initWithFrame_(
            NSMakeRect(MARGIN, y - 24, field_width - 90, 24)
        )
        self._file_field.setEditable_(False)
        self._file_field.setSelectable_(True)
        self._file_field.setPlaceholderString_("No file selected")
        content.addSubview_(self._file_field)

        self._browse_btn = self._make_button(
            "Browse\u2026", self._on_browse_clicked,
            NSMakeRect(MARGIN + field_width - 84, y - 26, 84, 28),
        )
        content.addSubview_(self._browse_btn)
        y -= 42

        # --- Speaker count ---
        y = self._add_label(content, "Speakers",
                            MARGIN, y, field_width, label_font, label_color)
        self._speakers_popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            NSMakeRect(MARGIN, y - 26, 180, 26), False
        )
        self._speakers_popup.addItemWithTitle_("Auto-detect")
        for n in range(1, 11):
            self._speakers_popup.addItemWithTitle_(f"{n} speaker{'s' if n > 1 else ''}")
        current = self._config.get("file_transcribe_num_speakers", 0) or 0
        if 0 <= current <= 10:
            self._speakers_popup.selectItemAtIndex_(current)
        content.addSubview_(self._speakers_popup)
        y -= 42

        # --- Progress bar + status ---
        self._progress = NSProgressIndicator.alloc().initWithFrame_(
            NSMakeRect(MARGIN, y - 18, field_width, 16)
        )
        self._progress.setStyle_(NSProgressIndicatorStyleBar)
        self._progress.setIndeterminate_(True)
        self._progress.setDisplayedWhenStopped_(False)
        content.addSubview_(self._progress)
        y -= 24

        self._status_label = NSTextField.alloc().initWithFrame_(
            NSMakeRect(MARGIN, y - 18, field_width, 18)
        )
        self._status_label.setBezeled_(False)
        self._status_label.setDrawsBackground_(False)
        self._status_label.setEditable_(False)
        self._status_label.setSelectable_(False)
        self._status_label.setFont_(NSFont.systemFontOfSize_(11.0))
        self._status_label.setTextColor_(label_color)
        self._status_label.setStringValue_("Ready")
        content.addSubview_(self._status_label)
        y -= 28

        # --- Results text view ---
        # Leaves space for the preview-format toggle (at y=52) and the
        # button row (at y=14).
        button_row_height = 96
        results_height = y - MARGIN - button_row_height
        if results_height < 100:
            results_height = 100

        scroll = NSScrollView.alloc().initWithFrame_(
            NSMakeRect(MARGIN, y - results_height, field_width, results_height)
        )
        scroll.setHasVerticalScroller_(True)
        scroll.setBorderType_(3)  # NSBezelBorder
        scroll.setAutohidesScrollers_(True)

        self._results_view = NSTextView.alloc().initWithFrame_(
            NSMakeRect(0, 0, field_width - 20, results_height)
        )
        self._results_view.setMinSize_(NSMakeSize(0, results_height))
        self._results_view.setMaxSize_(NSMakeSize(1e7, 1e7))
        self._results_view.setVerticallyResizable_(True)
        self._results_view.setHorizontallyResizable_(False)
        self._results_view.textContainer().setWidthTracksTextView_(True)
        self._results_view.setFont_(NSFont.userFixedPitchFontOfSize_(11.0))
        self._results_view.setEditable_(False)
        scroll.setDocumentView_(self._results_view)
        content.addSubview_(scroll)

        # --- Preview format toggle (above save buttons) ---
        seg_y = 52
        seg_w = 160
        seg_x = WINDOW_WIDTH - MARGIN - seg_w
        self._preview_segmented = NSSegmentedControl.alloc().initWithFrame_(
            NSMakeRect(seg_x, seg_y, seg_w, 24)
        )
        self._preview_segmented.setSegmentCount_(2)
        self._preview_segmented.setLabel_forSegment_("TXT preview", 0)
        self._preview_segmented.setLabel_forSegment_("SRT preview", 1)
        self._preview_segmented.setSelectedSegment_(0)
        _seg_target = _ActionTarget.alloc().initWithCallback_(
            self._on_preview_toggled
        )
        self._targets.append(_seg_target)
        self._preview_segmented.setTarget_(_seg_target)
        self._preview_segmented.setAction_("trigger:")
        content.addSubview_(self._preview_segmented)

        # Small "Preview:" label to the left of the segmented control
        prev_label = NSTextField.alloc().initWithFrame_(
            NSMakeRect(seg_x - 64, seg_y + 4, 60, 16)
        )
        prev_label.setStringValue_("Preview:")
        prev_label.setBezeled_(False)
        prev_label.setDrawsBackground_(False)
        prev_label.setEditable_(False)
        prev_label.setSelectable_(False)
        prev_label.setFont_(NSFont.systemFontOfSize_(11.0))
        prev_label.setTextColor_(NSColor.secondaryLabelColor())
        prev_label.setAlignment_(2)  # right-aligned
        content.addSubview_(prev_label)

        # --- Button row ---
        btn_y = 14
        btn_h = 30
        self._start_btn = self._make_button(
            "Start", self._on_start_clicked,
            NSMakeRect(MARGIN, btn_y, 80, btn_h),
        )
        self._start_btn.setEnabled_(False)
        self._start_btn.setKeyEquivalent_("\r")
        content.addSubview_(self._start_btn)

        self._cancel_btn = self._make_button(
            "Cancel", self._on_cancel_clicked,
            NSMakeRect(MARGIN + 86, btn_y, 80, btn_h),
        )
        self._cancel_btn.setEnabled_(False)
        content.addSubview_(self._cancel_btn)

        # Save/copy buttons right-aligned
        save_srt_x = WINDOW_WIDTH - MARGIN - 90
        self._save_srt_btn = self._make_button(
            "Save SRT\u2026", self._on_save_srt_clicked,
            NSMakeRect(save_srt_x, btn_y, 90, btn_h),
        )
        self._save_srt_btn.setEnabled_(False)
        content.addSubview_(self._save_srt_btn)

        save_txt_x = save_srt_x - 96
        self._save_txt_btn = self._make_button(
            "Save TXT\u2026", self._on_save_txt_clicked,
            NSMakeRect(save_txt_x, btn_y, 90, btn_h),
        )
        self._save_txt_btn.setEnabled_(False)
        content.addSubview_(self._save_txt_btn)

        copy_x = save_txt_x - 76
        self._copy_btn = self._make_button(
            "Copy", self._on_copy_clicked,
            NSMakeRect(copy_x, btn_y, 70, btn_h),
        )
        self._copy_btn.setEnabled_(False)
        content.addSubview_(self._copy_btn)

    def _add_label(self, parent, text, x, y, width, font, color):
        label = NSTextField.alloc().initWithFrame_(
            NSMakeRect(x, y, width, 16)
        )
        label.setStringValue_(text)
        label.setBezeled_(False)
        label.setDrawsBackground_(False)
        label.setEditable_(False)
        label.setSelectable_(False)
        label.setFont_(font)
        label.setTextColor_(color)
        parent.addSubview_(label)
        return y - 18

    def _make_button(self, title, callback, frame):
        btn = NSButton.alloc().initWithFrame_(frame)
        btn.setTitle_(title)
        btn.setBezelStyle_(NSBezelStyleRounded)
        target = _ActionTarget.alloc().initWithCallback_(callback)
        self._targets.append(target)
        btn.setTarget_(target)
        btn.setAction_("trigger:")
        return btn

    # --- Main-thread marshalling ---

    def _on_main(self, fn):
        """Run `fn` on the main thread."""
        dispatcher = _MainThreadCall.alloc().initWithCallable_(fn)
        self._main_thread_dispatchers.append(dispatcher)
        dispatcher.performSelectorOnMainThread_withObject_waitUntilDone_(
            "invoke:", None, False
        )

    # --- Actions ---

    def _on_browse_clicked(self, _sender):
        panel = NSOpenPanel.openPanel()
        panel.setCanChooseFiles_(True)
        panel.setCanChooseDirectories_(False)
        panel.setAllowsMultipleSelection_(False)
        panel.setAllowedFileTypes_(ft.SUPPORTED_EXTENSIONS)
        panel.setMessage_("Select an audio or video file to transcribe")
        if panel.runModal() != 1:  # NSModalResponseOK
            return
        urls = panel.URLs()
        if not urls:
            return
        path = str(urls[0].path())
        self._selected_file = path
        self._file_field.setStringValue_(path)
        self._start_btn.setEnabled_(True)
        self._refresh_setup_status()

    def _on_start_clicked(self, _sender):
        if not self._selected_file or not os.path.isfile(self._selected_file):
            self._show_status("Pick an audio file first", error=True)
            return

        if not ft.find_ffmpeg():
            self._show_status(
                "ffmpeg not found — install with: brew install ffmpeg",
                error=True,
            )
            return

        if not ft.is_asr_model_downloaded():
            self._show_status(
                "Parakeet ASR model not downloaded — open Settings "
                "and download it under Local Model",
                error=True,
            )
            return

        hf_token = self._config.get("hf_token", "").strip()
        if not hf_token:
            self._show_status(
                "Add your HuggingFace token in Settings first",
                error=True,
            )
            return

        # Deps missing -> offer to install, then auto-continue.
        if not deps_manager.are_deps_installed():
            self._prompt_and_install_deps()
            return

        self._run_transcription()

    def _prompt_and_install_deps(self):
        """Confirm with the user, then install file-transcribe deps."""
        alert = NSAlert.alloc().init()
        alert.setMessageText_("Install Transcription Libraries")
        alert.setInformativeText_(
            "File transcription needs pyannote.audio + torch, about 1.5 GB of "
            "extra packages. They install to ~/Library/Application Support/"
            "Scriber/python-deps/ (outside the app, so they survive updates).\n\n"
            "Install now? Transcription will start automatically once install "
            "finishes."
        )
        alert.addButtonWithTitle_("Install")
        alert.addButtonWithTitle_("Cancel")
        alert.setAlertStyle_(NSAlertStyleInformational)
        if alert.runModal() != NSAlertFirstButtonReturn:
            return

        self._set_running_ui("Installing dependencies\u2026")
        self._cancel_event = threading.Event()

        def on_progress(msg):
            self._on_main(lambda m=msg: self._status_label.setStringValue_(m))

        def on_complete():
            self._on_main(self._on_install_complete)

        def on_error(msg):
            self._on_main(lambda m=msg: self._on_install_error(m))

        deps_manager.install_deps(
            on_progress=on_progress,
            on_complete=on_complete,
            on_error=on_error,
            cancel_event=self._cancel_event,
        )

    def _on_install_complete(self):
        """Install finished — auto-continue to transcription."""
        self._reset_running_ui()
        self._status_label.setStringValue_(
            "Dependencies installed. Starting transcription\u2026"
        )
        self._run_transcription()

    def _on_install_error(self, error_msg: str):
        self._reset_running_ui()
        if error_msg == "Cancelled":
            self._status_label.setStringValue_("Install cancelled")
        else:
            self._show_status("Install failed: " + error_msg, error=True)

    def _run_transcription(self):
        """Kick off the actual pipeline in a background worker."""
        num_speakers = self._speakers_popup.indexOfSelectedItem()  # 0 = auto
        self._config["file_transcribe_num_speakers"] = int(num_speakers)
        try:
            self._save_config_callback(self._config)
        except Exception as e:
            logger.debug("Config save failed: %s", e)

        hf_token = self._config.get("hf_token", "").strip()

        # Reset results
        self._segments = []
        self._results_view.setString_("")
        self._copy_btn.setEnabled_(False)
        self._save_txt_btn.setEnabled_(False)
        self._save_srt_btn.setEnabled_(False)
        self._set_running_ui("Starting\u2026")

        self._cancel_event = threading.Event()

        def on_progress(msg):
            self._on_main(lambda m=msg: self._status_label.setStringValue_(m))

        def worker():
            try:
                segments = ft.run_pipeline(
                    self._selected_file,
                    hf_token=hf_token,
                    num_speakers=int(num_speakers),
                    on_progress=on_progress,
                    cancel_event=self._cancel_event,
                )
                self._on_main(lambda: self._on_pipeline_complete(segments))
            except Exception as e:
                import traceback
                logger.error("File transcription failed: %s\n%s",
                             e, traceback.format_exc())
                err = str(e)
                self._on_main(lambda: self._on_pipeline_error(err))

        self._worker_thread = threading.Thread(target=worker, daemon=True)
        self._worker_thread.start()

    def _set_running_ui(self, status: str):
        """Disable start/browse, enable cancel, show progress bar."""
        self._start_btn.setEnabled_(False)
        self._browse_btn.setEnabled_(False)
        self._speakers_popup.setEnabled_(False)
        self._cancel_btn.setEnabled_(True)
        self._progress.startAnimation_(None)
        self._status_label.setStringValue_(status)

    def _reset_running_ui(self):
        """Stop progress bar, re-enable controls."""
        self._progress.stopAnimation_(None)
        self._cancel_btn.setEnabled_(False)
        self._browse_btn.setEnabled_(True)
        self._speakers_popup.setEnabled_(True)
        self._start_btn.setEnabled_(True)

    def _on_cancel_clicked(self, _sender):
        if self._cancel_event is not None:
            self._cancel_event.set()
        self._status_label.setStringValue_("Cancelling\u2026")

    def _on_copy_clicked(self, _sender):
        text = self._format_current()
        if not text:
            return
        pb = NSPasteboard.generalPasteboard()
        pb.clearContents()
        pb.setString_forType_(text, NSPasteboardTypeString)
        self._status_label.setStringValue_(
            f"Copied to clipboard ({self._preview_format.upper()})"
        )

    def _on_preview_toggled(self, _sender):
        """Swap the results preview between TXT and SRT."""
        idx = self._preview_segmented.selectedSegment()
        self._preview_format = "srt" if idx == 1 else "txt"
        if self._segments:
            self._results_view.setString_(self._format_current())

    def _format_current(self) -> str:
        """Format the current segments in whatever preview mode is selected."""
        if not self._segments:
            return ""
        if self._preview_format == "srt":
            return ft.format_srt(self._segments)
        return ft.format_txt(self._segments)

    def _on_save_txt_clicked(self, _sender):
        self._save_to_file(ft.format_txt(self._segments), "txt", "transcript.txt")

    def _on_save_srt_clicked(self, _sender):
        self._save_to_file(ft.format_srt(self._segments), "srt", "transcript.srt")

    def _save_to_file(self, content: str, extension: str, default_name: str):
        if not content:
            return
        panel = NSSavePanel.savePanel()
        panel.setAllowedFileTypes_([extension])
        # Default name based on source file
        if self._selected_file:
            base = os.path.splitext(os.path.basename(self._selected_file))[0]
            panel.setNameFieldStringValue_(f"{base}.{extension}")
        else:
            panel.setNameFieldStringValue_(default_name)
        if panel.runModal() != 1:
            return
        url = panel.URL()
        if url is None:
            return
        path = str(url.path())
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            self._status_label.setStringValue_(f"Saved to {os.path.basename(path)}")
        except OSError as e:
            self._status_label.setStringValue_(f"Save failed: {e}")

    # --- Pipeline completion handlers (main thread) ---

    def _on_pipeline_complete(self, segments):
        self._reset_running_ui()

        self._segments = segments
        if not segments:
            self._status_label.setStringValue_("No speech detected")
            return

        self._results_view.setString_(self._format_current())
        self._copy_btn.setEnabled_(True)
        self._save_txt_btn.setEnabled_(True)
        self._save_srt_btn.setEnabled_(True)
        self._status_label.setStringValue_(
            f"Done — {len(segments)} segments, "
            f"{len({s.speaker for s in segments})} speaker(s)"
        )

    def _on_pipeline_error(self, error_msg: str):
        self._reset_running_ui()
        if error_msg == "Cancelled":
            self._status_label.setStringValue_("Cancelled")
        else:
            self._show_status(error_msg, error=True)

    def _show_status(self, msg: str, error: bool = False):
        # Truncate very long messages
        if len(msg) > 100:
            msg = msg[:100] + "\u2026"
        self._status_label.setStringValue_(msg)
        if error:
            self._status_label.setTextColor_(
                NSColor.colorWithRed_green_blue_alpha_(0.9, 0.3, 0.25, 1.0)
            )
        else:
            self._status_label.setTextColor_(NSColor.secondaryLabelColor())

    def _refresh_setup_status(self):
        """Summarize what's still needed in the status label."""
        missing = []
        if not ft.find_ffmpeg():
            missing.append("ffmpeg (brew install)")
        if not ft.is_asr_model_downloaded():
            missing.append("Parakeet")
        if not self._config.get("hf_token", "").strip():
            missing.append("HF token")
        if not ft.is_diarization_model_downloaded():
            missing.append("diarization model")
        if not deps_manager.are_deps_installed():
            missing.append("libraries")

        if missing:
            self._show_status(
                "Setup needed in Settings: " + ", ".join(missing),
                error=False,
            )
        elif self._selected_file and os.path.isfile(self._selected_file):
            self._show_status("Ready \u2014 click Start", error=False)
        else:
            self._show_status("Ready \u2014 pick a file", error=False)

    # --- Public API ---

    def update_config(self, config: dict):
        self._config = config

    def load_file(self, path: str):
        """Pre-populate the file field with `path` and enable Start.

        Used by ScribeApp after a meeting recording is saved — drops the
        user straight into the transcription flow for that file.
        """
        if not path or not os.path.isfile(path):
            return
        self._selected_file = path
        self._file_field.setStringValue_(path)
        self._start_btn.setEnabled_(True)
        self._refresh_setup_status()

    def show(self):
        # Refresh setup state unless a task is currently running
        if not self._cancel_btn.isEnabled():
            self._refresh_setup_status()
        NSApp.activateIgnoringOtherApps_(True)
        self._window.makeKeyAndOrderFront_(None)
