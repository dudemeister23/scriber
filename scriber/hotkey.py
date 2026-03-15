"""Global hotkey using NSEvent global monitors.

Uses NSEvent.addGlobalMonitorForEventsMatchingMask_ which does NOT require
Accessibility permission (unlike CGEventTap). This is a passive monitor —
it observes events without consuming them, which is fine for our use case.

Supports two modes:
- Hold mode: hold a modifier key (e.g. Control) to record, release to transcribe.
  Requires minimum hold duration to avoid accidental triggers.
- Toggle mode: press a key combo (e.g. Cmd+Shift+Space) to toggle recording.
"""

import time
import logging
from typing import Callable

from AppKit import NSEvent, NSFlagsChangedMask, NSKeyDownMask
from Quartz import (
    kCGEventFlagMaskCommand,
    kCGEventFlagMaskShift,
    kCGEventFlagMaskAlternate,
    kCGEventFlagMaskControl,
)

logger = logging.getLogger("scriber")

# Minimum hold duration (seconds) to trigger transcription in hold mode
MIN_HOLD_DURATION = 0.3

# Virtual keycodes for common keys
KEYCODE_MAP = {
    "space": 49,
    "return": 36,
    "tab": 48,
    "escape": 53,
    "delete": 51,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118,
    "f5": 96, "f6": 97, "f7": 98, "f8": 100,
    "f9": 101, "f10": 109, "f11": 103, "f12": 111,
    "a": 0, "b": 11, "c": 8, "d": 2, "e": 14, "f": 3,
    "g": 5, "h": 4, "i": 34, "j": 38, "k": 40, "l": 37,
    "m": 46, "n": 45, "o": 31, "p": 35, "q": 12, "r": 15,
    "s": 1, "t": 17, "u": 32, "v": 9, "w": 13, "x": 7,
    "y": 16, "z": 6,
    "1": 18, "2": 19, "3": 20, "4": 21, "5": 23,
    "6": 22, "7": 26, "8": 28, "9": 25, "0": 29,
}

MODIFIER_MAP = {
    "cmd": kCGEventFlagMaskCommand,
    "cmd_r": kCGEventFlagMaskCommand,
    "command": kCGEventFlagMaskCommand,
    "shift": kCGEventFlagMaskShift,
    "alt": kCGEventFlagMaskAlternate,
    "option": kCGEventFlagMaskAlternate,
    "ctrl": kCGEventFlagMaskControl,
    "control": kCGEventFlagMaskControl,
}

# NSEvent modifier flag equivalents
NS_MODIFIER_MAP = {
    "cmd": 1 << 20,       # NSEventModifierFlagCommand
    "cmd_r": 1 << 20,
    "command": 1 << 20,
    "shift": 1 << 17,     # NSEventModifierFlagShift
    "alt": 1 << 19,       # NSEventModifierFlagOption
    "option": 1 << 19,
    "ctrl": 1 << 18,      # NSEventModifierFlagControl
    "control": 1 << 18,
}

HOLD_MODIFIERS = {"cmd", "cmd_r", "command", "shift", "alt", "option", "ctrl", "control"}


def parse_hotkey(hotkey_str: str):
    """Parse a hotkey string. Returns either:
    - ("hold", modifier_flag) for a single modifier like "control"
    - ("toggle", keycode, modifier_mask) for a combo like "cmd+shift+space"
    """
    parts = [p.strip().lower() for p in hotkey_str.replace("+", " ").split()]

    if len(parts) == 1 and parts[0] in HOLD_MODIFIERS:
        return ("hold", NS_MODIFIER_MAP[parts[0]])

    keycode = None
    modifiers = 0
    for part in parts:
        if part in NS_MODIFIER_MAP:
            modifiers |= NS_MODIFIER_MAP[part]
        elif part in KEYCODE_MAP:
            keycode = KEYCODE_MAP[part]
        else:
            raise ValueError(f"Unknown key: {part}")

    if keycode is None:
        raise ValueError(f"No key found in hotkey string: {hotkey_str}")

    return ("toggle", keycode, modifiers)


class GlobalHotkey:
    """Listens for a global hotkey using NSEvent global monitors.

    No Accessibility permission required — this is a passive observer.

    Hold mode: on_press when modifier pressed, on_release when released
               (if held >= MIN_HOLD_DURATION), on_cancel if released too quickly.
    Toggle mode: alternates between on_press and on_release on each key combo press.
    """

    def __init__(
        self,
        hotkey_str: str,
        on_press: Callable,
        on_release: Callable,
        on_cancel: Callable,
    ):
        parsed = parse_hotkey(hotkey_str)
        self._mode = parsed[0]
        self._on_press = on_press
        self._on_release = on_release
        self._on_cancel = on_cancel
        self._monitors = []

        if self._mode == "hold":
            self._modifier_flag = parsed[1]
            self._held = False
            self._press_time = 0.0
        else:
            self._keycode = parsed[1]
            self._modifiers = parsed[2]
            self._toggled = False

    def start(self):
        """Register global event monitors. Must be called from the main thread
        (which it is, since main.py calls this before app.run())."""
        if self._mode == "hold":
            monitor = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
                NSFlagsChangedMask,
                self._handle_flags_changed,
            )
            if monitor is None:
                logger.error("Failed to register flags-changed monitor")
                return
            self._monitors.append(monitor)
            logger.info("Hold-mode monitor registered (modifier_flag=0x%x)", self._modifier_flag)
        else:
            # Toggle mode needs both key-down and flags-changed monitors
            monitor_key = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
                NSKeyDownMask,
                self._handle_key_down,
            )
            if monitor_key:
                self._monitors.append(monitor_key)
            logger.info("Toggle-mode monitor registered (keycode=%d, modifiers=0x%x)",
                        self._keycode, self._modifiers)

    def _handle_flags_changed(self, event):
        """Handle modifier key press/release for hold mode."""
        flags = event.modifierFlags()
        modifier_active = bool(flags & self._modifier_flag)

        if modifier_active and not self._held:
            self._held = True
            self._press_time = time.monotonic()
            logger.debug("Modifier pressed")
            self._on_press()
        elif not modifier_active and self._held:
            self._held = False
            duration = time.monotonic() - self._press_time
            logger.debug("Modifier released after %.2fs", duration)
            if duration >= MIN_HOLD_DURATION:
                self._on_release()
            else:
                self._on_cancel()

    def _handle_key_down(self, event):
        """Handle key-down for toggle mode."""
        keycode = event.keyCode()
        flags = event.modifierFlags()

        # Mask to just the modifier bits we care about
        relevant_flags = flags & (
            NS_MODIFIER_MAP["cmd"]
            | NS_MODIFIER_MAP["shift"]
            | NS_MODIFIER_MAP["alt"]
            | NS_MODIFIER_MAP["ctrl"]
        )

        if keycode == self._keycode and relevant_flags == self._modifiers:
            if not self._toggled:
                self._toggled = True
                self._on_press()
            else:
                self._toggled = False
                self._on_release()

    def stop(self):
        for monitor in self._monitors:
            NSEvent.removeMonitor_(monitor)
        self._monitors.clear()
