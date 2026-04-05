"""Scriber menubar application."""

import logging
import os
import subprocess
import threading
import time
import webbrowser

import objc
import rumps
from AVFoundation import (
    AVCaptureDevice,
    AVMediaTypeAudio,
    AVAuthorizationStatusAuthorized,
    AVAuthorizationStatusNotDetermined,
)
from Foundation import NSObject

from . import __version__
from .audio import AudioRecorder
from .config import CONFIG_FILE, load_config, save_config, get_api_key
from .overlay import RecordingOverlay
from .paste import paste_text, PasteError, _check_accessibility
from .settings_window import SettingsWindowController
from .local_transcribe import is_model_downloaded, transcribe_local, MODELS as LOCAL_MODELS, DEFAULT_MODEL as LOCAL_DEFAULT_MODEL
from .streaming import StreamingTranscriber
from .transcribe import transcribe
from . import updater

logger = logging.getLogger("scriber")


def _resource_path(filename: str) -> str:
    """Resolve path to a bundled resource, with fallback for dev mode."""
    bundle_dir = os.environ.get("RESOURCEPATH", "")
    if bundle_dir:
        p = os.path.join(bundle_dir, "icons", filename)
        if os.path.exists(p):
            return p
    return os.path.join(os.path.dirname(__file__), "icons", filename)


ICON_IDLE = _resource_path("mic_idle.png")
ICON_RECORDING = _resource_path("mic_recording.png")


class _MainThreadDispatcher(NSObject):
    """Runs a queued callable on the main thread via performSelectorOnMainThread_."""

    def init(self):
        self = objc.super(_MainThreadDispatcher, self).init()
        if self is None:
            return None
        self._queue = []
        self._lock = threading.Lock()
        return self

    def enqueue_(self, callable_):
        with self._lock:
            self._queue.append(callable_)
        self.performSelectorOnMainThread_withObject_waitUntilDone_(
            "drain:", None, False
        )

    def drain_(self, _sender):
        while True:
            with self._lock:
                if not self._queue:
                    return
                fn = self._queue.pop(0)
            try:
                fn()
            except Exception as e:
                logger.error("Main-thread callback failed: %s", e)


class ScribeApp(rumps.App):
    def __init__(self):
        has_icons = os.path.exists(ICON_IDLE)
        super().__init__(
            name="Scriber",
            title=None if has_icons else "\U0001f399",
            icon=ICON_IDLE if has_icons else None,
            template=True,
            quit_button=None,
        )
        self.config = load_config()
        self.recorder = AudioRecorder()
        self._transcribing = False
        self._overlay = RecordingOverlay(self.recorder)
        self._settings_controller = None

        # Streaming state
        self._streaming_session = None

        # Last transcription (for re-pasting)
        self._last_transcription = None

        # Build menu
        self.status_item = rumps.MenuItem("Ready")
        self.status_item.set_callback(None)

        self._mode_menu = rumps.MenuItem("Mode")
        self._mode_items = {}
        for label, value in [
            ("Batch (Scribe V2)", "batch"),
            ("Streaming (Scribe V2 RT)", "streaming"),
            ("Local (on-device)", "local"),
        ]:
            item = rumps.MenuItem(label, callback=self._select_mode)
            item._mode_value = value
            self._mode_items[value] = item
            self._mode_menu.add(item)
        self._update_mode_checkmarks()

        self._paste_last_item = rumps.MenuItem(
            "Paste Last Transcription", callback=self._paste_last_transcription
        )
        self._paste_last_item.set_callback(None)  # disabled until first transcription

        self.menu = [
            self.status_item,
            None,
            self._paste_last_item,
            None,
            self._mode_menu,
            rumps.MenuItem("Settings\u2026", callback=self._open_settings),
            rumps.MenuItem("Check for Updates\u2026", callback=self._check_for_updates_clicked),
            None,
            rumps.MenuItem(f"Scriber v{__version__}"),
            None,
            rumps.MenuItem("Quit Scriber", callback=self.quit_app),
        ]

        # Update state
        self._update_check_in_progress = False
        self._update_installing = False
        self._main_dispatcher = _MainThreadDispatcher.alloc().init()

        # Check Accessibility permission at startup (prompts user if missing)
        rumps.Timer(self._check_accessibility_once, 1.0).start()

        # Prompt for API key on first launch
        if not get_api_key(self.config):
            # Defer to after the run loop starts
            rumps.Timer(self._prompt_api_key_once, 0.5).start()

        # Auto-check for updates 10s after startup (silent — only prompts if one is found)
        rumps.Timer(self._auto_update_check_once, 10.0).start()

    def _check_accessibility_once(self, timer):
        timer.stop()
        if not _check_accessibility(prompt=True):
            logger.warning("Accessibility permission not yet granted -- prompting user")

    def _prompt_api_key_once(self, timer):
        timer.stop()
        if not get_api_key(self.config):
            self._open_settings(None)

    # --- Recording lifecycle (called by hotkey module) ---

    def _check_mic_permission(self) -> bool:
        """Check microphone authorization. Returns True if authorized."""
        status = AVCaptureDevice.authorizationStatusForMediaType_(AVMediaTypeAudio)
        if status == AVAuthorizationStatusAuthorized:
            return True
        if status == AVAuthorizationStatusNotDetermined:
            # Request permission once — don't start recording during the prompt
            logger.info("Requesting microphone permission")
            self._requesting_permission = True
            AVCaptureDevice.requestAccessForMediaType_completionHandler_(
                AVMediaTypeAudio,
                lambda granted: setattr(self, '_requesting_permission', False),
            )
            return False
        # Denied or restricted
        rumps.notification(
            "Scriber",
            "Microphone Access Required",
            "Grant microphone access in System Settings > Privacy & Security > Microphone.",
        )
        return False

    def start_recording(self, _sender=None):
        """Called when the hotkey is pressed (hold) or toggled on."""
        logger.info("start_recording called (transcribing=%s, recording=%s)",
                     self._transcribing, self.recorder.is_recording)
        if self._transcribing or self.recorder.is_recording:
            return
        if getattr(self, '_requesting_permission', False):
            return
        if not self._check_mic_permission():
            return

        try:
            device_name = self.config.get("input_device", "")
            device_index = AudioRecorder.resolve_device_name(device_name)
            logger.info("Starting recording (device=%s, index=%s)", device_name or "default", device_index)
            self.recorder.start(device=device_index)
        except Exception as e:
            rumps.notification("Scriber", "Microphone Error", str(e))
            logger.error("Failed to start recording: %s", e)
            return

        self.status_item.title = "Recording\u2026"
        if os.path.exists(ICON_RECORDING):
            self.icon = ICON_RECORDING
        else:
            self.title = "\U0001f534"

        mode = self.config.get("mode", "batch")
        logger.info("Recording mode: %s", mode)
        if mode == "streaming":
            self._start_streaming()
        elif mode == "local":
            local_model_key = self.config.get("local_model", LOCAL_DEFAULT_MODEL)
            if not is_model_downloaded(local_model_key):
                self.recorder.stop()
                self._overlay.show("Recording\u2026")
                model_label = LOCAL_MODELS.get(local_model_key, {}).get("label", local_model_key)
                self._overlay.show_error(f"{model_label} not downloaded \u2014 open Settings")
                self._reset_ui()
                return
            self._overlay.show("Recording\u2026")
        else:
            self._overlay.show("Recording\u2026")

    def stop_and_transcribe(self, _sender=None):
        """Called when the hotkey is released (hold) or toggled off."""
        logger.info("stop_and_transcribe called (recording=%s)", self.recorder.is_recording)
        if not self.recorder.is_recording:
            return

        # Streaming mode — different teardown
        if self._streaming_session is not None:
            self._stop_streaming()
            return

        # Batch / Local mode
        self._transcribing = True
        self.status_item.title = "Transcribing\u2026"

        audio_data = self.recorder.stop()

        if os.path.exists(ICON_IDLE):
            self.icon = ICON_IDLE
        else:
            self.title = "\u231b"

        mode = self.config.get("mode", "batch")
        if mode == "local":
            self._overlay.update_status("Transcribing locally\u2026")
            thread = threading.Thread(target=self._do_transcribe_local, args=(audio_data,), daemon=True)
        else:
            self._overlay.update_status("Transcribing\u2026")
            thread = threading.Thread(target=self._do_transcribe, args=(audio_data,), daemon=True)
        thread.start()

    def cancel_recording(self, _sender=None):
        """Called when the hotkey is released too quickly (hold mode)."""
        logger.info("cancel_recording called (recording=%s)", self.recorder.is_recording)
        if not self.recorder.is_recording:
            return

        # Streaming mode — clean teardown
        if self._streaming_session is not None:
            self._cancel_streaming()
            return

        self.recorder.stop()  # Discard audio
        self._overlay.hide()
        self._reset_ui()

    # --- Batch mode internal ---

    def _do_transcribe(self, audio_data: bytes):
        try:
            api_key = get_api_key(self.config)
            if not api_key:
                self._overlay.show_error("No API key — open Settings")
                self._reset_ui()
                self._transcribing = False
                return

            # Start elapsed-time updater
            start_time = time.time()
            self._transcribe_done = False

            def _update_elapsed():
                while not self._transcribe_done:
                    elapsed = int(time.time() - start_time)
                    if elapsed >= 3:
                        self._overlay.update_status(f"Transcribing\u2026 {elapsed}s")
                    time.sleep(1.0)

            timer_thread = threading.Thread(target=_update_elapsed, daemon=True)
            timer_thread.start()

            text = transcribe(
                audio_data,
                api_key,
                language=self.config.get("language", ""),
                keyterms=self.config.get("keyterms"),
            )
            self._transcribe_done = True

            if text:
                # API omits trailing period on final sentence — add one if missing
                stripped = text.rstrip()
                if stripped and stripped[-1].isalnum():
                    text = stripped + "."
                logger.info("Transcript received (%d chars): %s", len(text), text[:80])
                self._save_last_transcription(text)
                try:
                    paste_text(text + " ")
                    logger.info("Paste completed")
                except PasteError as pe:
                    logger.error("Paste failed: %s", pe)
                    self._overlay.show_error("Paste failed — check Accessibility permission")
                    return
                self._overlay.hide()
            else:
                self._overlay.show_error("No speech detected")
        except Exception as e:
            self._transcribe_done = True
            error_msg = str(e)
            logger.error("Transcription failed: %s", e)
            # Show a user-friendly error in the overlay
            if "500" in error_msg:
                self._overlay.show_error("API server error — try again")
            elif "401" in error_msg or "403" in error_msg:
                self._overlay.show_error("Invalid API key")
            elif "timeout" in error_msg.lower() or "timed out" in error_msg.lower():
                self._overlay.show_error("Request timed out — try again")
            elif "certificate" in error_msg.lower() or "ssl" in error_msg.lower():
                self._overlay.show_error("SSL error — check connection")
            else:
                # Truncate long errors
                short = error_msg[:60] + "\u2026" if len(error_msg) > 60 else error_msg
                self._overlay.show_error(short)
        finally:
            self._transcribe_done = True
            self._transcribing = False
            self._reset_ui()

    # --- Local mode internal ---

    def _do_transcribe_local(self, audio_data: bytes):
        try:
            # Start elapsed-time updater
            start_time = time.time()
            self._transcribe_done = False

            def _update_elapsed():
                while not self._transcribe_done:
                    elapsed = int(time.time() - start_time)
                    if elapsed >= 3:
                        self._overlay.update_status(f"Transcribing locally\u2026 {elapsed}s")
                    time.sleep(1.0)

            timer_thread = threading.Thread(target=_update_elapsed, daemon=True)
            timer_thread.start()

            local_model_key = self.config.get("local_model", LOCAL_DEFAULT_MODEL)
            fast_mode = self.config.get("local_fast_mode", False)
            text = transcribe_local(
                audio_data,
                language=self.config.get("language", ""),
                model_key=local_model_key,
                fast_mode=fast_mode,
                keyterms=self.config.get("keyterms", []),
            )
            self._transcribe_done = True

            if text:
                # Add trailing period if missing
                stripped = text.rstrip()
                if stripped and stripped[-1].isalnum():
                    text = stripped + "."
                logger.info("Local transcript (%d chars): %s", len(text), text[:80])
                self._save_last_transcription(text)
                try:
                    paste_text(text + " ")
                    logger.info("Paste completed")
                except PasteError as pe:
                    logger.error("Paste failed: %s", pe)
                    self._overlay.show_error("Paste failed \u2014 check Accessibility permission")
                    return
                self._overlay.hide()
            else:
                self._overlay.show_error("No speech detected")
        except Exception as e:
            self._transcribe_done = True
            import traceback
            error_msg = str(e)
            logger.error("Local transcription failed: %s\n%s", e, traceback.format_exc())
            short = error_msg[:60] + "\u2026" if len(error_msg) > 60 else error_msg
            self._overlay.show_error(short)
        finally:
            self._transcribe_done = True
            self._transcribing = False
            self._reset_ui()

    # --- Streaming mode ---

    def _start_streaming(self):
        """Begin a streaming transcription session."""
        api_key = get_api_key(self.config)
        if not api_key:
            rumps.notification("Scriber", "No API Key", "Please set your API key in Settings.")
            self.recorder.stop()
            self._reset_ui()
            return

        try:
            self._streaming_session = StreamingTranscriber(
                api_key=api_key,
                language=self.config.get("language", ""),
                on_partial=self._on_streaming_partial,
                on_committed=self._on_streaming_committed,
                on_error=self._on_streaming_error,
            )
            self._streaming_session.start()
        except Exception as e:
            logger.error("Failed to start streaming: %s", e)
            rumps.notification("Scriber", "Streaming Error", str(e))
            self._streaming_session = None
            self.recorder.stop()
            self._overlay.hide()
            self._reset_ui()
            return

        # Hook up audio chunks to the streamer
        self.recorder.set_on_chunk(self._streaming_session.send_chunk)

        self._overlay.show("Streaming\u2026", streaming=True)
        logger.info("Streaming session started")

    def _stop_streaming(self):
        """End a streaming session normally (on hotkey release).

        Flushes remaining audio, waits for the final transcript,
        then pastes the full accumulated text at once.
        """
        logger.info("Stopping streaming session")

        # Detach audio hook and stop recorder immediately
        self.recorder.set_on_chunk(None)
        self.recorder.stop()

        self._overlay.update_status("Finishing\u2026")

        # Flush + wait for final transcript in background to avoid blocking UI
        session = self._streaming_session
        self._streaming_session = None
        thread = threading.Thread(target=self._finish_streaming, args=(session,), daemon=True)
        thread.start()

    def _finish_streaming(self, session):
        """Background thread: flush the streaming session, then paste everything."""
        try:
            if session:
                session.stop()  # sends commit, waits for final transcript
        except Exception as e:
            logger.error("Streaming flush error: %s", e)

        # Collect the full transcript from the overlay
        full_text = self._overlay.get_full_transcript().strip()
        if full_text:
            # Add trailing period if missing
            if full_text[-1].isalnum():
                full_text += "."
            logger.info("Streaming final paste (%d chars): %s", len(full_text), full_text[:80])
            self._save_last_transcription(full_text)
            try:
                paste_text(full_text + " ")
            except PasteError as pe:
                logger.error("Streaming paste failed: %s", pe)
                self._overlay.show_error("Paste failed — check Accessibility permission")
                self._reset_ui()
                return
        else:
            self._overlay.show_error("No speech detected")
            self._reset_ui()
            return

        self._overlay.hide()
        self._reset_ui()

    def _cancel_streaming(self):
        """Cancel a streaming session (too-short hold)."""
        logger.info("Cancelling streaming session")
        self.recorder.set_on_chunk(None)
        self.recorder.stop()

        if self._streaming_session:
            self._streaming_session.stop()
            self._streaming_session = None

        self._overlay.hide()
        self._reset_ui()

    def _on_streaming_partial(self, text: str):
        """Called from WebSocket recv thread with interim transcript."""
        self._overlay.set_partial_text(text)

    def _on_streaming_committed(self, text: str):
        """Called from WebSocket recv thread with finalized transcript segment."""
        logger.info("Streaming committed segment: %s", text[:80])
        # Add trailing period if missing
        stripped = text.rstrip()
        if stripped and stripped[-1].isalnum():
            text = stripped + "."
        # Accumulate in overlay — will be pasted all at once on release
        self._overlay.append_committed_text(text)

    def _on_streaming_error(self, error_msg: str):
        """Called from WebSocket recv thread on API error."""
        logger.error("Streaming error: %s", error_msg)
        short = error_msg[:60] + "\u2026" if len(error_msg) > 60 else error_msg
        self._overlay.show_error(short)

    # --- Last transcription ---

    def _save_last_transcription(self, text: str):
        """Store the transcription so it can be re-pasted from the menu."""
        self._last_transcription = text
        # Enable the menu item
        self._paste_last_item.set_callback(self._paste_last_transcription)

    def _paste_last_transcription(self, _sender=None):
        """Re-paste the most recent transcription."""
        if not self._last_transcription:
            return
        try:
            paste_text(self._last_transcription + " ")
            logger.info("Re-pasted last transcription (%d chars)", len(self._last_transcription))
        except PasteError as pe:
            logger.error("Re-paste failed: %s", pe)
            rumps.notification("Scriber", "Paste Failed", "Check Accessibility permission.")

    # --- UI ---

    def _reset_ui(self):
        self.status_item.title = "Ready"
        if os.path.exists(ICON_IDLE):
            self.icon = ICON_IDLE
        else:
            self.title = "\U0001f399"

    # --- Mode selection ---

    def _update_mode_checkmarks(self):
        current = self.config.get("mode", "batch")
        for value, item in self._mode_items.items():
            item.state = 1 if value == current else 0

    def _select_mode(self, sender):
        new_mode = getattr(sender, "_mode_value", "batch")
        self.config["mode"] = new_mode
        save_config(self.config)
        self._update_mode_checkmarks()
        logger.info("Mode selected: %s", new_mode)

    # --- Settings ---

    def _open_settings(self, sender):
        if self._settings_controller is None:
            self._settings_controller = SettingsWindowController(
                config=self.config,
                save_callback=self._apply_settings,
            )
        else:
            self._settings_controller.update_config(self.config)
        self._settings_controller.show()

    def _apply_settings(self, new_config: dict):
        old_hotkey = self.config.get("hotkey")
        self.config = new_config
        save_config(self.config)
        self._update_mode_checkmarks()
        logger.info("Settings saved: mode=%s, hotkey=%s, device=%s",
                     new_config.get("mode"), new_config.get("hotkey"),
                     new_config.get("input_device", "default"))

        if new_config.get("hotkey") != old_hotkey:
            rumps.notification(
                "Scriber",
                "Hotkey Changed",
                "Restart Scriber to apply the new hotkey.",
            )

    # --- Updates ---

    UPDATE_CHECK_INTERVAL = 24 * 60 * 60  # 24 hours

    def _auto_update_check_once(self, timer):
        timer.stop()
        last = self.config.get("last_update_check", 0) or 0
        if time.time() - last < self.UPDATE_CHECK_INTERVAL:
            logger.debug("Skipping auto update check (last check < 24h ago)")
            return
        self._start_update_check(silent=True)

    def _check_for_updates_clicked(self, _sender):
        """Manual 'Check for Updates…' menu invocation."""
        self._start_update_check(silent=False)

    def _start_update_check(self, silent: bool):
        if self._update_check_in_progress or self._update_installing:
            return
        self._update_check_in_progress = True
        logger.info("Checking for updates (silent=%s)", silent)
        thread = threading.Thread(
            target=self._do_update_check, args=(silent,), daemon=True
        )
        thread.start()

    def _do_update_check(self, silent: bool):
        try:
            release = updater.check_for_update(__version__)
            self.config["last_update_check"] = int(time.time())
            save_config(self.config)
            self._on_main(lambda: self._handle_update_result(release, silent))
        except Exception as e:
            logger.error("Update check error: %s", e)
            if not silent:
                self._on_main(
                    lambda: rumps.alert(
                        title="Update Check Failed",
                        message=f"Could not reach GitHub: {e}",
                    )
                )
        finally:
            self._update_check_in_progress = False

    def _handle_update_result(self, release, silent: bool):
        """Main thread: show the appropriate dialog based on check result."""
        if release is None:
            if not silent:
                rumps.alert(
                    title="Scriber is up to date",
                    message=f"You're running v{__version__} (the latest).",
                )
            return

        # Suppress auto-prompt for a version the user has skipped.
        if silent and self.config.get("skipped_update_version") == release.version:
            logger.info("Skipping prompt for %s (user deferred)", release.version)
            return

        self._prompt_update(release)

    def _prompt_update(self, release):
        has_installable = bool(release.asset_url) and updater.is_running_from_bundle()

        notes = updater.format_notes(release.notes, limit=400)
        message = f"Scriber {release.tag_name} is available. You have v{__version__}."
        if notes:
            message += "\n\nRelease notes:\n" + notes

        if has_installable:
            ok = "Install & Restart"
            cancel = "Later"
            other = "View on GitHub"
        elif release.asset_url and not updater.is_running_from_bundle():
            # Dev mode — no auto-install
            message += "\n\n(Running from source — auto-install is disabled.)"
            ok = "View on GitHub"
            cancel = "Later"
            other = None
        else:
            # No zip asset — send user to GitHub
            message += "\n\nThis release doesn't include a pre-built binary."
            ok = "View on GitHub"
            cancel = "Later"
            other = None

        result = rumps.alert(
            title="Update Available",
            message=message,
            ok=ok,
            cancel=cancel,
            other=other,
        )

        if has_installable:
            if result == 1:  # Install & Restart
                self._start_update_install(release)
            elif result == -1:  # View on GitHub (other)
                webbrowser.open(release.html_url)
            else:  # Later
                self.config["skipped_update_version"] = release.version
                save_config(self.config)
        else:
            if result == 1:  # View on GitHub
                webbrowser.open(release.html_url)
            else:
                self.config["skipped_update_version"] = release.version
                save_config(self.config)

    def _start_update_install(self, release):
        """Kick off download + install in a background thread."""
        if self._update_installing:
            return
        self._update_installing = True
        self.status_item.title = "Downloading update\u2026"
        thread = threading.Thread(
            target=self._do_update_install, args=(release,), daemon=True
        )
        thread.start()

    def _do_update_install(self, release):
        try:
            bundle_path = updater.get_bundle_path()
            if not bundle_path:
                raise RuntimeError("Could not locate the running .app bundle")

            dest = os.path.join(
                os.path.expanduser("~/Library/Application Support/Scriber/updates"),
                release.asset_name or "Scriber.app.zip",
            )
            os.makedirs(os.path.dirname(dest), exist_ok=True)

            # Throttle title updates so we don't hammer the main thread
            last_pct = [-1]

            def on_progress(received, total):
                if total <= 0:
                    return
                pct = int(received * 100 / total)
                if pct != last_pct[0]:
                    last_pct[0] = pct
                    self._on_main(
                        lambda p=pct: setattr(
                            self.status_item, "title", f"Downloading update\u2026 {p}%"
                        )
                    )

            updater.download_release(release.asset_url, dest, progress_callback=on_progress)

            self._on_main(lambda: setattr(self.status_item, "title", "Installing update\u2026"))
            updater.stage_and_install(dest, bundle_path)

            # Clear the skipped-version guard so the user isn't stuck if the install fails
            self.config["skipped_update_version"] = ""
            save_config(self.config)

            # Give the helper script a moment to start waiting, then quit.
            time.sleep(0.3)
            self._on_main(self.quit_app)
        except Exception as e:
            logger.error("Update install failed: %s", e)
            self._update_installing = False
            self._on_main(lambda: self._reset_ui())
            self._on_main(
                lambda: rumps.alert(
                    title="Update Failed",
                    message=f"Could not install update: {e}",
                )
            )

    def _on_main(self, action):
        """Run `action` on the main thread. Safe to call from any thread."""
        self._main_dispatcher.enqueue_(action)

    def quit_app(self, sender=None):
        if self._streaming_session:
            self.recorder.set_on_chunk(None)
            self._streaming_session.stop()
            self._streaming_session = None
        if self.recorder.is_recording:
            self.recorder.stop()
        self._overlay.hide()
        rumps.quit_application()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    app = ScribeApp()
    app.run()
