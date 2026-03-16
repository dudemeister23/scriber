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


def paste_text(text: str):
    """Write text to pasteboard and simulate Cmd+V to paste it.

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

    # Save current pasteboard contents
    pb = NSPasteboard.generalPasteboard()
    old_contents = pb.stringForType_(NSPasteboardTypeString)

    # Set new text
    pb.clearContents()
    pb.setString_forType_(text, NSPasteboardTypeString)

    # Small delay to ensure pasteboard is ready
    time.sleep(0.05)

    # Simulate Cmd+V via CGEvent (requires Accessibility permission)
    try:
        # Key down
        event_down = CGEventCreateKeyboardEvent(None, _kVK_V, True)
        if event_down is None:
            raise PasteError(
                "Cannot create keyboard event — Accessibility permission may have been revoked. "
                "Text is on clipboard — use Cmd+V to paste manually. "
                "Re-grant in System Settings → Privacy & Security → Accessibility."
            )
        CGEventSetFlags(event_down, kCGEventFlagMaskCommand)
        CGEventPost(kCGHIDEventTap, event_down)

        # Key up
        event_up = CGEventCreateKeyboardEvent(None, _kVK_V, False)
        CGEventSetFlags(event_up, kCGEventFlagMaskCommand)
        CGEventPost(kCGHIDEventTap, event_up)

        logger.info("Paste keystroke sent via CGEvent")
    except PasteError:
        raise
    except Exception as e:
        logger.error("CGEvent paste failed: %s", e)
        raise PasteError(
            f"Paste keystroke failed: {e}. "
            "Text is on clipboard — use Cmd+V to paste manually."
        )

    # Wait for the target app to process the paste before restoring clipboard
    time.sleep(0.5)
    if old_contents is not None:
        pb.clearContents()
        pb.setString_forType_(old_contents, NSPasteboardTypeString)

    logger.info("Paste completed")


# --- Streaming helpers ---

def save_clipboard():
    """Save current clipboard contents. Call before a streaming session."""
    pb = NSPasteboard.generalPasteboard()
    return pb.stringForType_(NSPasteboardTypeString)


def restore_clipboard(old_contents):
    """Restore clipboard contents. Call after a streaming session."""
    if old_contents is not None:
        pb = NSPasteboard.generalPasteboard()
        pb.clearContents()
        pb.setString_forType_(old_contents, NSPasteboardTypeString)


def _send_cmd_v():
    """Simulate Cmd+V keystroke via CGEvent."""
    event_down = CGEventCreateKeyboardEvent(None, _kVK_V, True)
    CGEventSetFlags(event_down, kCGEventFlagMaskCommand)
    CGEventPost(kCGHIDEventTap, event_down)

    event_up = CGEventCreateKeyboardEvent(None, _kVK_V, False)
    CGEventSetFlags(event_up, kCGEventFlagMaskCommand)
    CGEventPost(kCGHIDEventTap, event_up)


def paste_text_streaming(text: str):
    """Paste text during a streaming session (no clipboard save/restore).

    Faster than paste_text — skips save/restore since the caller manages
    the clipboard lifecycle via save_clipboard/restore_clipboard.
    """
    pb = NSPasteboard.generalPasteboard()
    pb.clearContents()
    pb.setString_forType_(text, NSPasteboardTypeString)

    time.sleep(0.03)

    try:
        _send_cmd_v()
    except Exception as e:
        logger.error("Streaming paste failed: %s", e)

    # Short delay for target app to process
    time.sleep(0.1)
