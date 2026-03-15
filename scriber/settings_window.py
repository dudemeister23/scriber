"""Settings window using PyObjC / AppKit."""

import objc
from AppKit import (
    NSApp,
    NSBackingStoreBuffered,
    NSBezelStyleRounded,
    NSButton,
    NSColor,
    NSFont,
    NSPopUpButton,
    NSScreen,
    NSScrollView,
    NSSecureTextField,
    NSTextField,
    NSTextView,
    NSView,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskTitled,
)
from Foundation import NSMakeRect, NSMakeSize, NSObject

from .audio import AudioRecorder

# Hotkey presets: (display label, config value)
HOTKEY_PRESETS = [
    ("Hold Control", "control"),
    ("Hold Option", "option"),
    ("Hold Command", "command"),
    ("Cmd+Shift+Space (toggle)", "cmd+shift+space"),
]

MODE_PRESETS = [
    ("Batch \u2014 Scribe V2 (transcribe after recording)", "batch"),
    ("Streaming \u2014 Scribe V2 RT (real-time, as you speak)", "streaming"),
]

WINDOW_WIDTH = 420
WINDOW_HEIGHT = 482


class _SaveButtonTarget(NSObject):
    """NSObject-based target for the Save button action."""

    def initWithController_(self, controller):
        self = objc.super(_SaveButtonTarget, self).init()
        if self is None:
            return None
        self._controller = controller
        return self

    def saveClicked_(self, sender):
        self._controller._save_clicked_(sender)


class SettingsWindowController:
    """Manages the Scriber settings window."""

    def __init__(self, config: dict, save_callback):
        self._config = config
        self._save_callback = save_callback
        self._window = None
        self._api_key_field = None
        self._hotkey_popup = None
        self._mode_popup = None
        self._device_popup = None
        self._language_field = None
        self._keyterms_view = None
        self._save_target = _SaveButtonTarget.alloc().initWithController_(self)
        self._build_window()

    def _build_window(self):
        rect = NSMakeRect(0, 0, WINDOW_WIDTH, WINDOW_HEIGHT)
        style = NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
        self._window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, style, NSBackingStoreBuffered, False
        )
        self._window.setTitle_("Scriber Settings")
        self._window.center()
        self._window.setReleasedWhenClosed_(False)

        content = self._window.contentView()
        y = WINDOW_HEIGHT - 50  # Start from top, working down
        margin = 20
        field_width = WINDOW_WIDTH - 2 * margin
        label_font = NSFont.systemFontOfSize_(11.0)
        label_color = NSColor.secondaryLabelColor()

        # --- API Key ---
        y = self._add_label(content, "ElevenLabs API Key", margin, y, field_width, label_font, label_color)
        self._api_key_field = NSSecureTextField.alloc().initWithFrame_(
            NSMakeRect(margin, y - 24, field_width, 24)
        )
        self._api_key_field.setStringValue_(self._config.get("api_key", ""))
        self._api_key_field.setPlaceholderString_("sk_...")
        content.addSubview_(self._api_key_field)
        y -= 40

        # --- Hotkey ---
        y = self._add_label(content, "Hotkey", margin, y, field_width, label_font, label_color)
        self._hotkey_popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            NSMakeRect(margin, y - 26, field_width, 26), False
        )
        current_hotkey = self._config.get("hotkey", "control")
        for label, value in HOTKEY_PRESETS:
            self._hotkey_popup.addItemWithTitle_(label)
        # Select current
        for i, (label, value) in enumerate(HOTKEY_PRESETS):
            if value == current_hotkey:
                self._hotkey_popup.selectItemAtIndex_(i)
                break
        content.addSubview_(self._hotkey_popup)
        y -= 42

        # --- Mode ---
        y = self._add_label(content, "Mode", margin, y, field_width, label_font, label_color)
        self._mode_popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            NSMakeRect(margin, y - 26, field_width, 26), False
        )
        current_mode = self._config.get("mode", "batch")
        for label, value in MODE_PRESETS:
            self._mode_popup.addItemWithTitle_(label)
        for i, (label, value) in enumerate(MODE_PRESETS):
            if value == current_mode:
                self._mode_popup.selectItemAtIndex_(i)
                break
        content.addSubview_(self._mode_popup)
        y -= 42

        # --- Input Device ---
        y = self._add_label(content, "Input Device", margin, y, field_width, label_font, label_color)
        self._device_popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            NSMakeRect(margin, y - 26, field_width, 26), False
        )
        self._device_popup.addItemWithTitle_("System Default")
        self._input_devices = AudioRecorder.get_input_devices()
        current_device = self._config.get("input_device", "")
        selected_idx = 0
        for i, dev in enumerate(self._input_devices):
            self._device_popup.addItemWithTitle_(dev["name"])
            if dev["name"] == current_device:
                selected_idx = i + 1  # +1 because "System Default" is index 0
        self._device_popup.selectItemAtIndex_(selected_idx)
        content.addSubview_(self._device_popup)
        y -= 42

        # --- Language ---
        y = self._add_label(content, "Language (ISO code, empty = auto-detect)", margin, y, field_width, label_font, label_color)
        self._language_field = NSTextField.alloc().initWithFrame_(
            NSMakeRect(margin, y - 24, field_width, 24)
        )
        self._language_field.setStringValue_(self._config.get("language", ""))
        self._language_field.setPlaceholderString_("auto-detect")
        content.addSubview_(self._language_field)
        y -= 40

        # --- Keyterms ---
        y = self._add_label(content, "Keyterms (one per line, max 100)", margin, y, field_width, label_font, label_color)
        scroll_height = max(y - 60, 80)
        scroll_view = NSScrollView.alloc().initWithFrame_(
            NSMakeRect(margin, y - scroll_height, field_width, scroll_height)
        )
        scroll_view.setHasVerticalScroller_(True)
        scroll_view.setBorderType_(3)  # NSBezelBorder

        self._keyterms_view = NSTextView.alloc().initWithFrame_(
            NSMakeRect(0, 0, field_width - 20, scroll_height)
        )
        self._keyterms_view.setMinSize_(NSMakeSize(0, scroll_height))
        self._keyterms_view.setMaxSize_(NSMakeSize(1e7, 1e7))
        self._keyterms_view.setVerticallyResizable_(True)
        self._keyterms_view.setHorizontallyResizable_(False)
        self._keyterms_view.textContainer().setWidthTracksTextView_(True)
        self._keyterms_view.setFont_(NSFont.systemFontOfSize_(12.0))
        keyterms_text = "\n".join(self._config.get("keyterms", []))
        self._keyterms_view.setString_(keyterms_text)

        scroll_view.setDocumentView_(self._keyterms_view)
        content.addSubview_(scroll_view)
        y -= scroll_height + 16

        # --- Save button ---
        save_btn = NSButton.alloc().initWithFrame_(
            NSMakeRect(WINDOW_WIDTH - margin - 90, 14, 90, 32)
        )
        save_btn.setTitle_("Save")
        save_btn.setBezelStyle_(NSBezelStyleRounded)
        save_btn.setTarget_(self._save_target)
        save_btn.setAction_("saveClicked:")
        content.addSubview_(save_btn)

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

    def _save_clicked_(self, sender):
        # Read all fields
        api_key = str(self._api_key_field.stringValue()).strip()
        hotkey_idx = self._hotkey_popup.indexOfSelectedItem()
        hotkey_value = HOTKEY_PRESETS[hotkey_idx][1] if 0 <= hotkey_idx < len(HOTKEY_PRESETS) else "control"

        device_idx = self._device_popup.indexOfSelectedItem()
        if device_idx == 0:
            input_device = ""
        else:
            dev_list_idx = device_idx - 1
            if 0 <= dev_list_idx < len(self._input_devices):
                input_device = self._input_devices[dev_list_idx]["name"]
            else:
                input_device = ""

        mode_idx = self._mode_popup.indexOfSelectedItem()
        mode_value = MODE_PRESETS[mode_idx][1] if 0 <= mode_idx < len(MODE_PRESETS) else "batch"

        language = str(self._language_field.stringValue()).strip()

        keyterms_text = str(self._keyterms_view.string())
        keyterms = [t.strip() for t in keyterms_text.split("\n") if t.strip()][:100]

        new_config = {
            "api_key": api_key,
            "hotkey": hotkey_value,
            "mode": mode_value,
            "input_device": input_device,
            "language": language,
            "keyterms": keyterms,
        }

        self._save_callback(new_config)
        self._config = new_config
        self._window.close()

    def update_config(self, config: dict):
        """Refresh fields with current config values."""
        self._config = config
        self._api_key_field.setStringValue_(config.get("api_key", ""))
        self._language_field.setStringValue_(config.get("language", ""))
        self._keyterms_view.setString_("\n".join(config.get("keyterms", [])))

        # Update hotkey selection
        current_hotkey = config.get("hotkey", "control")
        for i, (label, value) in enumerate(HOTKEY_PRESETS):
            if value == current_hotkey:
                self._hotkey_popup.selectItemAtIndex_(i)
                break

        # Update mode selection
        current_mode = config.get("mode", "batch")
        for i, (label, value) in enumerate(MODE_PRESETS):
            if value == current_mode:
                self._mode_popup.selectItemAtIndex_(i)
                break

        # Update device list and selection
        self._device_popup.removeAllItems()
        self._device_popup.addItemWithTitle_("System Default")
        self._input_devices = AudioRecorder.get_input_devices()
        current_device = config.get("input_device", "")
        selected_idx = 0
        for i, dev in enumerate(self._input_devices):
            self._device_popup.addItemWithTitle_(dev["name"])
            if dev["name"] == current_device:
                selected_idx = i + 1
        self._device_popup.selectItemAtIndex_(selected_idx)

    def show(self):
        NSApp.activateIgnoringOtherApps_(True)
        self._window.makeKeyAndOrderFront_(None)
