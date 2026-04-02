"""Paste text into the currently focused text field via the macOS pasteboard."""

import logging
import time

from AppKit import NSPasteboard, NSPasteboardTypeString
from ApplicationServices import AXIsProcessTrustedWithOptions
from CoreFoundation import CFDictionaryCreate, kCFBooleanTrue
from Quartz import (
    CGEventCreateKeyboardEvent,
    CGEventSetFlags,
    CGEventPost,
    kCGEventFlagMaskCommand,
    kCGHIDEventTap,
)

logger = logging.getLogger("scriber")

# Virtual keycode for 'V'
_kVK_V = 0x09

# CoreFoundation key for the prompt option
_kAXTrustedCheckOptionPrompt = "AXTrustedCheckOptionPrompt"


def _check_accessibility(prompt: bool = False) -> bool:
    """Check if the app has Accessibility permission.

    If prompt=True and permission is missing, macOS will show the
    system dialog directing the user to System Settings.
    """
    options = {_kAXTrustedCheckOptionPrompt: prompt}
    trusted = AXIsProcessTrustedWithOptions(options)
    return bool(trusted)


class PasteError(Exception):
    """Raised when paste fails due to permissions or other issues."""
    pass


def _save_pasteboard():
    """Save all pasteboard items with all their types (string, image, file, etc.)."""
    pb = NSPasteboard.generalPasteboard()
    items = pb.pasteboardItems()
    if not items:
        return None

    saved = []
    for item in items:
        item_data = {}
        for t in item.types():
            data = item.dataForType_(t)
            if data:
                item_data[t] = data
        if item_data:
            saved.append(item_data)
    return saved or None


def _restore_pasteboard(saved):
    """Restore pasteboard from previously saved items."""
    pb = NSPasteboard.generalPasteboard()
    if not saved:
        pb.clearContents()
        return

    from AppKit import NSPasteboardItem
    new_items = []
    for item_data in saved:
        item = NSPasteboardItem.alloc().init()
        for type_name, data in item_data.items():
            item.setData_forType_(data, type_name)
        new_items.append(item)

    pb.clearContents()
    pb.writeObjects_(new_items)


def paste_text(text: str):
    """Write text to pasteboard and simulate Cmd+V to paste it.

    Saves the full pasteboard state (all types) before pasting and restores
    it afterwards, so the user's clipboard is not disrupted.

    Raises PasteError if Accessibility permission is missing or the paste
    appears to have failed silently.
    """
    # Check Accessibility permission; prompt user if missing
    if not _check_accessibility(prompt=True):
        logger.error("Accessibility permission not granted.")
        # Still put text on clipboard so the user can paste manually
        pb = NSPasteboard.generalPasteboard()
        pb.clearContents()
        pb.setString_forType_(text, NSPasteboardTypeString)
        raise PasteError(
            "Accessibility permission not granted. "
            "Text is on clipboard — use Cmd+V to paste manually. "
            "Re-grant in System Settings → Privacy & Security → Accessibility."
        )

    # Save full pasteboard state (all types: string, image, file, etc.)
    saved = _save_pasteboard()

    try:
        # Set new text
        pb = NSPasteboard.generalPasteboard()
        pb.clearContents()
        pb.setString_forType_(text, NSPasteboardTypeString)

        # Small delay to ensure pasteboard is ready
        time.sleep(0.05)

        # Simulate Cmd+V via CGEvent (requires Accessibility permission)
        event_down = CGEventCreateKeyboardEvent(None, _kVK_V, True)
        if event_down is None:
            raise PasteError(
                "Cannot create keyboard event — Accessibility permission may have been revoked. "
                "Text is on clipboard — use Cmd+V to paste manually. "
                "Re-grant in System Settings → Privacy & Security → Accessibility."
            )
        CGEventSetFlags(event_down, kCGEventFlagMaskCommand)
        CGEventPost(kCGHIDEventTap, event_down)

        event_up = CGEventCreateKeyboardEvent(None, _kVK_V, False)
        CGEventSetFlags(event_up, kCGEventFlagMaskCommand)
        CGEventPost(kCGHIDEventTap, event_up)

        logger.info("Paste keystroke sent via CGEvent")

        # Wait for the target app to process the paste before restoring clipboard
        time.sleep(0.5)
    finally:
        _restore_pasteboard(saved)

    logger.info("Paste completed")
