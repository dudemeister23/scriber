"""Floating recording overlay HUD positioned above the dock."""

import logging
import math
import random
import textwrap

import objc
from AppKit import (
    NSBackingStoreBuffered,
    NSBezierPath,
    NSColor,
    NSFont,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSPanel,
    NSParagraphStyleAttributeName,
    NSMutableParagraphStyle,
    NSScreen,
    NSView,
)
from Foundation import NSMakeRect, NSMakePoint, NSMakeSize, NSObject, NSString, NSTimer

logger = logging.getLogger("scriber")

# Panel style constants
NSWindowStyleMaskBorderless = 0
NSWindowStyleMaskNonactivatingPanel = 1 << 7
NSWindowCollectionBehaviorCanJoinAllSpaces = 1 << 0
NSWindowCollectionBehaviorStationary = 1 << 4

OVERLAY_WIDTH = 360
OVERLAY_HEIGHT = 52  # Batch mode / initial streaming height
CORNER_RADIUS = 14

# Streaming transcript layout
STREAM_TEXT_MARGIN = 16
STREAM_TEXT_WIDTH = OVERLAY_WIDTH - 2 * STREAM_TEXT_MARGIN
STREAM_LINE_HEIGHT = 16.0
STREAM_MAX_HEIGHT = 400  # Don't grow taller than this
STREAM_STATUS_BAR_HEIGHT = 44  # Top bar with dot + waveform + status
STREAM_TEXT_PADDING_BOTTOM = 12

# Waveform config
NUM_BARS = 24
BAR_WIDTH = 3.0
BAR_GAP = 2.0
BAR_MIN_HEIGHT = 2.0
BAR_MAX_HEIGHT = 26.0
SMOOTHING = 0.25  # Lower = smoother (exponential moving average)

# Approximate chars per line for word-wrapping
CHARS_PER_LINE = int(STREAM_TEXT_WIDTH / 7)


class OverlayContentView(NSView):
    """Custom NSView that draws the recording HUD with waveform."""

    def initWithFrame_(self, frame):
        self = objc.super(OverlayContentView, self).initWithFrame_(frame)
        if self is None:
            return None
        self._rms_level = 0.0
        self._status_text = "Recording\u2026"
        self._pulse_phase = 0.0
        self._is_recording = True
        self._is_error = False
        self._streaming_mode = False
        self._tick = 0

        # Streaming transcript state
        self._committed_text = ""   # All committed segments joined
        self._partial_text = ""     # Current interim text

        # Per-bar state for smooth animation
        self._bar_targets = [0.0] * NUM_BARS
        self._bar_current = [0.0] * NUM_BARS
        self._bar_phase = [random.uniform(0, math.tau) for _ in range(NUM_BARS)]
        self._bar_speed = [random.uniform(0.6, 1.4) for _ in range(NUM_BARS)]
        return self

    def isFlipped(self):
        return False

    def drawRect_(self, rect):
        bounds = self.bounds()
        w = bounds.size.width
        h = bounds.size.height

        # Background with subtle border
        bg_path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            bounds, CORNER_RADIUS, CORNER_RADIUS
        )
        NSColor.colorWithRed_green_blue_alpha_(0.08, 0.08, 0.10, 0.92).set()
        bg_path.fill()

        # Subtle border
        NSColor.colorWithRed_green_blue_alpha_(1.0, 1.0, 1.0, 0.08).set()
        bg_path.setLineWidth_(0.5)
        bg_path.stroke()

        # --- Status bar area (top of overlay) ---
        status_center_y = h - STREAM_STATUS_BAR_HEIGHT / 2.0

        # Recording dot with glow
        dot_radius = 4.5
        dot_x = 18.0
        dot_y = status_center_y

        if self._is_error:
            # Error state — red dot, no pulse
            NSColor.colorWithRed_green_blue_alpha_(1.0, 0.2, 0.15, 0.9).set()
        elif self._is_recording:
            pulse = 0.5 + 0.5 * math.sin(self._pulse_phase)
            self._pulse_phase += 0.12

            # Glow behind dot
            glow_radius = dot_radius + 4.0 + pulse * 3.0
            NSColor.colorWithRed_green_blue_alpha_(1.0, 0.25, 0.2, 0.15 + pulse * 0.1).set()
            glow_path = NSBezierPath.bezierPathWithOvalInRect_(
                NSMakeRect(dot_x - glow_radius, dot_y - glow_radius,
                           glow_radius * 2, glow_radius * 2)
            )
            glow_path.fill()

            # Dot
            NSColor.colorWithRed_green_blue_alpha_(1.0, 0.28, 0.23, 0.7 + pulse * 0.3).set()
        else:
            # Transcribing/finishing state — amber dot
            pulse = 0.5 + 0.5 * math.sin(self._pulse_phase)
            self._pulse_phase += 0.18
            NSColor.colorWithRed_green_blue_alpha_(1.0, 0.76, 0.0, 0.7 + pulse * 0.3).set()

        dot_path = NSBezierPath.bezierPathWithOvalInRect_(
            NSMakeRect(dot_x - dot_radius, dot_y - dot_radius,
                       dot_radius * 2, dot_radius * 2)
        )
        dot_path.fill()

        if self._is_recording:
            # --- Waveform bars ---
            self._tick += 1
            level = self._rms_level

            waveform_total_width = NUM_BARS * BAR_WIDTH + (NUM_BARS - 1) * BAR_GAP
            waveform_x = w - waveform_total_width - 14.0
            waveform_center_y = dot_y

            for i in range(NUM_BARS):
                phase = self._bar_phase[i] + self._tick * 0.08 * self._bar_speed[i]
                wave = 0.3 + 0.7 * (0.5 + 0.5 * math.sin(phase))
                target = level * wave
                target += random.uniform(-0.02, 0.02)
                target = max(0.0, min(1.0, target))
                self._bar_targets[i] = target

            for i in range(NUM_BARS):
                diff = self._bar_targets[i] - self._bar_current[i]
                self._bar_current[i] += diff * SMOOTHING

            for i in range(NUM_BARS):
                val = self._bar_current[i]
                bar_h = BAR_MIN_HEIGHT + val * (BAR_MAX_HEIGHT - BAR_MIN_HEIGHT)
                bar_x = waveform_x + i * (BAR_WIDTH + BAR_GAP)
                bar_y = waveform_center_y - bar_h / 2.0

                bar_rect = NSMakeRect(bar_x, bar_y, BAR_WIDTH, bar_h)
                bar_path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                    bar_rect, BAR_WIDTH / 2, BAR_WIDTH / 2
                )

                brightness = 0.4 + val * 0.6
                alpha = 0.5 + val * 0.5
                NSColor.colorWithRed_green_blue_alpha_(
                    0.15 * brightness,
                    0.85 * brightness,
                    0.95 * brightness,
                    alpha,
                ).set()
                bar_path.fill()

                if val > 0.3:
                    glow_rect = NSMakeRect(
                        bar_x - 1.0, bar_y - 1.0,
                        BAR_WIDTH + 2.0, bar_h + 2.0
                    )
                    glow_path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                        glow_rect, (BAR_WIDTH + 2.0) / 2, (BAR_WIDTH + 2.0) / 2
                    )
                    glow_alpha = (val - 0.3) * 0.25
                    NSColor.colorWithRed_green_blue_alpha_(
                        0.2, 0.85, 1.0, glow_alpha
                    ).set()
                    glow_path.fill()

        # Status text (next to dot)
        if self._is_error:
            text_color = NSColor.colorWithRed_green_blue_alpha_(1.0, 0.4, 0.35, 0.95)
        else:
            text_color = NSColor.colorWithRed_green_blue_alpha_(1.0, 1.0, 1.0, 0.85)
        status_attrs = {
            NSFontAttributeName: NSFont.systemFontOfSize_(11.0),
            NSForegroundColorAttributeName: text_color,
        }
        text = NSString.stringWithString_(self._status_text)
        text_size = text.sizeWithAttributes_(status_attrs)
        text_x = dot_x + dot_radius + 10.0
        text_y = dot_y - text_size.height / 2.0
        text.drawAtPoint_withAttributes_(NSMakePoint(text_x, text_y), status_attrs)

        # --- Streaming transcript area (below status bar) ---
        if self._streaming_mode and (self._committed_text or self._partial_text):
            # Separator line below status bar
            sep_y = h - STREAM_STATUS_BAR_HEIGHT
            NSColor.colorWithRed_green_blue_alpha_(1.0, 1.0, 1.0, 0.06).set()
            sep_path = NSBezierPath.bezierPath()
            sep_path.moveToPoint_(NSMakePoint(STREAM_TEXT_MARGIN, sep_y))
            sep_path.lineToPoint_(NSMakePoint(w - STREAM_TEXT_MARGIN, sep_y))
            sep_path.setLineWidth_(0.5)
            sep_path.stroke()

            # Draw committed text (white)
            y_cursor = sep_y - 8  # Start below separator

            if self._committed_text:
                committed_attrs = {
                    NSFontAttributeName: NSFont.systemFontOfSize_(12.0),
                    NSForegroundColorAttributeName: NSColor.colorWithRed_green_blue_alpha_(
                        1.0, 1.0, 1.0, 0.9
                    ),
                }
                # Word-wrap the committed text
                lines = _wrap_text(self._committed_text, CHARS_PER_LINE)
                for line in lines:
                    y_cursor -= STREAM_LINE_HEIGHT
                    if y_cursor < STREAM_TEXT_PADDING_BOTTOM:
                        break
                    ns_line = NSString.stringWithString_(line)
                    ns_line.drawAtPoint_withAttributes_(
                        NSMakePoint(STREAM_TEXT_MARGIN, y_cursor),
                        committed_attrs,
                    )

            # Draw partial text (dimmer, after committed)
            if self._partial_text:
                partial_attrs = {
                    NSFontAttributeName: NSFont.systemFontOfSize_(12.0),
                    NSForegroundColorAttributeName: NSColor.colorWithRed_green_blue_alpha_(
                        0.6, 0.85, 1.0, 0.6
                    ),
                }
                lines = _wrap_text(self._partial_text, CHARS_PER_LINE)
                for line in lines:
                    y_cursor -= STREAM_LINE_HEIGHT
                    if y_cursor < STREAM_TEXT_PADDING_BOTTOM:
                        break
                    ns_line = NSString.stringWithString_(line)
                    ns_line.drawAtPoint_withAttributes_(
                        NSMakePoint(STREAM_TEXT_MARGIN, y_cursor),
                        partial_attrs,
                    )


def _wrap_text(text, width):
    """Word-wrap text into lines of approximately `width` characters."""
    if not text:
        return []
    lines = []
    for paragraph in text.split("\n"):
        wrapped = textwrap.wrap(paragraph, width=width) if paragraph.strip() else [""]
        lines.extend(wrapped)
    return lines


def _calc_streaming_height(committed_text, partial_text):
    """Calculate the panel height needed to fit the current transcript."""
    total_lines = 0
    if committed_text:
        total_lines += len(_wrap_text(committed_text, CHARS_PER_LINE))
    if partial_text:
        total_lines += len(_wrap_text(partial_text, CHARS_PER_LINE))

    text_height = total_lines * STREAM_LINE_HEIGHT + 8 + STREAM_TEXT_PADDING_BOTTOM
    height = STREAM_STATUS_BAR_HEIGHT + text_height
    # Clamp between minimum and maximum
    height = max(OVERLAY_HEIGHT, min(height, STREAM_MAX_HEIGHT))
    return height


class _OverlayTimerTarget(NSObject):
    """ObjC-compatible timer target that polls audio level."""

    def initWithOverlay_(self, overlay):
        self = objc.super(_OverlayTimerTarget, self).init()
        if self is None:
            return None
        self._overlay = overlay
        return self

    def tick_(self, timer):
        ov = self._overlay
        if ov and ov._recorder:
            level = ov._recorder.rms_level
            ov._content_view._rms_level = level

            # In streaming mode, resize panel to fit transcript
            if ov._content_view._streaming_mode:
                ov._resize_to_fit()

            ov._content_view.setNeedsDisplay_(True)


class _OverlayHideTarget(NSObject):
    """ObjC target for performing hide on main thread."""

    def initWithOverlay_(self, overlay):
        self = objc.super(_OverlayHideTarget, self).init()
        if self is None:
            return None
        self._overlay = overlay
        return self

    def doHide_(self, sender):
        self._overlay._do_hide()


class RecordingOverlay:
    """Floating HUD that shows recording state and audio level."""

    def __init__(self, recorder):
        self._recorder = recorder
        self._timer = None
        self._panel = None
        self._content_view = None
        self._current_height = OVERLAY_HEIGHT
        self._timer_target = _OverlayTimerTarget.alloc().initWithOverlay_(self)
        self._hide_target = _OverlayHideTarget.alloc().initWithOverlay_(self)
        self._setup_panel()

    def _setup_panel(self):
        rect = NSMakeRect(0, 0, OVERLAY_WIDTH, OVERLAY_HEIGHT)

        self._panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            rect,
            NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
            NSBackingStoreBuffered,
            False,
        )
        self._panel.setLevel_(25)
        self._panel.setOpaque_(False)
        self._panel.setBackgroundColor_(NSColor.clearColor())
        self._panel.setIgnoresMouseEvents_(True)
        self._panel.setHasShadow_(True)
        self._panel.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces | NSWindowCollectionBehaviorStationary
        )

        self._content_view = OverlayContentView.alloc().initWithFrame_(rect)
        self._panel.setContentView_(self._content_view)

    def _position_panel(self, height=None):
        """Position the panel centered above the dock. Grows upward."""
        screen = NSScreen.mainScreen()
        if screen is None:
            return
        if height is None:
            height = self._current_height
        visible = screen.visibleFrame()
        x = visible.origin.x + (visible.size.width - OVERLAY_WIDTH) / 2
        y = visible.origin.y + 20
        self._panel.setFrame_display_(
            NSMakeRect(x, y, OVERLAY_WIDTH, height), True
        )
        self._content_view.setFrameSize_(NSMakeSize(OVERLAY_WIDTH, height))
        self._current_height = height

    def _resize_to_fit(self):
        """Resize the panel to fit the current transcript text. Grows upward."""
        cv = self._content_view
        needed = _calc_streaming_height(cv._committed_text, cv._partial_text)
        # Only resize if height changed meaningfully (avoid constant jitter)
        if abs(needed - self._current_height) >= STREAM_LINE_HEIGHT * 0.5:
            self._position_panel(needed)

    def show(self, status="Recording\u2026", streaming=False):
        self._content_view._status_text = status
        self._content_view._is_recording = True
        self._content_view._is_error = False
        self._content_view._streaming_mode = streaming
        self._content_view._committed_text = ""
        self._content_view._partial_text = ""
        self._content_view._pulse_phase = 0.0
        self._content_view._rms_level = 0.0
        self._content_view._tick = 0
        # Reset bar state for fresh animation
        self._content_view._bar_current = [0.0] * NUM_BARS
        self._content_view._bar_targets = [0.0] * NUM_BARS
        self._content_view._bar_phase = [random.uniform(0, math.tau) for _ in range(NUM_BARS)]
        self._content_view._bar_speed = [random.uniform(0.6, 1.4) for _ in range(NUM_BARS)]

        self._position_panel(OVERLAY_HEIGHT)
        self._panel.orderFrontRegardless()
        self._start_timer()
        logger.debug("Overlay shown: %s (streaming=%s)", status, streaming)

    def update_status(self, status):
        self._content_view._status_text = status
        self._content_view._is_recording = False
        self._content_view.setNeedsDisplay_(True)
        logger.debug("Overlay status: %s", status)

    def set_partial_text(self, text):
        """Update the streaming partial (interim) transcript text."""
        self._content_view._partial_text = text
        self._content_view.setNeedsDisplay_(True)

    def append_committed_text(self, text):
        """Append a committed transcript segment."""
        if self._content_view._committed_text:
            self._content_view._committed_text += " " + text
        else:
            self._content_view._committed_text = text
        # Clear partial since it's now committed
        self._content_view._partial_text = ""
        self._content_view.setNeedsDisplay_(True)

    def get_full_transcript(self):
        """Return the full accumulated transcript (committed + partial)."""
        parts = []
        if self._content_view._committed_text:
            parts.append(self._content_view._committed_text)
        if self._content_view._partial_text:
            parts.append(self._content_view._partial_text)
        return " ".join(parts) if parts else ""

    def show_error(self, message):
        """Show an error message in the overlay, then auto-hide after 3 seconds."""
        self._content_view._status_text = message
        self._content_view._is_recording = False
        self._content_view._is_error = True
        self._content_view.setNeedsDisplay_(True)
        logger.debug("Overlay error: %s", message)
        # Auto-hide after 3 seconds
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            3.0, self._hide_target, "doHide:", None, False
        )

    def hide(self):
        """Hide the overlay. Safe to call from any thread."""
        self._hide_target.performSelectorOnMainThread_withObject_waitUntilDone_(
            "doHide:", None, False
        )

    def _do_hide(self):
        """Actually hide — must be called on the main thread."""
        self._stop_timer()
        self._panel.orderOut_(None)
        logger.debug("Overlay hidden")

    def _start_timer(self):
        self._stop_timer()
        self._timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            1.0 / 24.0,  # ~24 fps for smooth waveform
            self._timer_target,
            "tick:",
            None,
            True,
        )

    def _stop_timer(self):
        if self._timer is not None:
            self._timer.invalidate()
            self._timer = None
