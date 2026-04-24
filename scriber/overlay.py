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
OVERLAY_HEIGHT = 48  # Animation-only height
CORNER_RADIUS = 14
ZAP_MIN_HEIGHT = 4.0
OPEN_ANIMATION_FRAMES = 8
CLOSE_RETURN_FRAMES = 5
CLOSE_DIE_FRAMES = 4
CLOSE_CURTAIN_FRAMES = 8
CLOSE_ANIMATION_FRAMES = CLOSE_RETURN_FRAMES + CLOSE_DIE_FRAMES + CLOSE_CURTAIN_FRAMES
TRANSCRIBE_SETTLE_FRAMES = 12
TRANSCRIBE_COLOR_FRAMES = 16

# Streaming transcript layout
STREAM_TEXT_MARGIN = 16
STREAM_TEXT_WIDTH = OVERLAY_WIDTH - 2 * STREAM_TEXT_MARGIN
STREAM_LINE_HEIGHT = 16.0
STREAM_MAX_HEIGHT = 400  # Don't grow taller than this
ANIMATION_HEIGHT = OVERLAY_HEIGHT
MESSAGE_BAR_HEIGHT = 24
STREAM_TEXT_PADDING_BOTTOM = 12

# Waveform config
NUM_BARS = 58
BAR_WIDTH = 3.0
BAR_GAP = 2.0
BAR_MIN_HEIGHT = 2.0
BAR_MAX_HEIGHT = 28.0
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
        self._message_text = ""
        self._pulse_phase = 0.0
        self._is_recording = True
        self._is_error = False
        self._streaming_mode = False
        self._tick = 0
        self._opening_start_tick = None
        self._opening_active = False
        self._transcribe_start_tick = 0
        self._completion_start_tick = None
        self._completion_start_travel = 0.0
        self._completion_active = False

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
        completion_elapsed = 0
        completion_return = 0.0
        completion_die = 0.0
        completion_fade = 0.0
        if self._completion_active and self._completion_start_tick is not None:
            completion_elapsed = max(0, self._tick - self._completion_start_tick)
            completion_return = min(1.0, completion_elapsed / CLOSE_RETURN_FRAMES)
            die_elapsed = max(0, completion_elapsed - CLOSE_RETURN_FRAMES)
            completion_die = min(1.0, die_elapsed / CLOSE_DIE_FRAMES)
            completion_fade = _ease_in_out(max(0.0, (completion_die - 0.20) / 0.80))

        # Background with subtle border
        radius_x = min(CORNER_RADIUS, max(0.0, w / 2.0))
        radius_y = min(CORNER_RADIUS, max(0.0, h / 2.0))
        bg_path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            bounds, radius_x, radius_y
        )
        NSColor.colorWithRed_green_blue_alpha_(0.08, 0.08, 0.10, 0.92).set()
        bg_path.fill()

        # Subtle border
        NSColor.colorWithRed_green_blue_alpha_(1.0, 1.0, 1.0, 0.08).set()
        bg_path.setLineWidth_(0.5)
        bg_path.stroke()

        # --- Visual state area (top of overlay) ---
        layout_width = OVERLAY_WIDTH
        layout_x = (w - layout_width) / 2.0
        layout_height = OVERLAY_HEIGHT if h < OVERLAY_HEIGHT else h
        layout_y = (h - OVERLAY_HEIGHT) / 2.0 if h < OVERLAY_HEIGHT else 0.0
        animation_center_y = layout_y + layout_height - ANIMATION_HEIGHT / 2.0
        horizontal_padding = 16.0
        animation_left = layout_x + horizontal_padding
        animation_right = layout_x + layout_width - horizontal_padding
        animation_width = animation_right - animation_left

        if self._is_recording:
            self._tick += 1
            level = max(self._rms_level, 0.08)
            waveform_total_width = NUM_BARS * BAR_WIDTH + (NUM_BARS - 1) * BAR_GAP
            waveform_x = animation_left + (animation_width - waveform_total_width) / 2.0

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
                bar_y = animation_center_y - bar_h / 2.0

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
        else:
            # Transcribing keeps the recording waveform shape, then sends
            # yellow waves from the center outward and back.
            self._tick += 1
            elapsed = max(0, self._tick - self._transcribe_start_tick)
            settle = min(1.0, elapsed / TRANSCRIBE_SETTLE_FRAMES)
            handoff = min(1.0, elapsed / TRANSCRIBE_COLOR_FRAMES)
            if self._completion_active:
                return_ease = _ease_in_out(completion_return)
                travel = self._completion_start_travel * (1.0 - return_ease)
            else:
                travel = _transcribe_wave_travel(elapsed)
            eased = 0.5 - 0.5 * math.cos(travel * math.pi)

            waveform_total_width = NUM_BARS * BAR_WIDTH + (NUM_BARS - 1) * BAR_GAP
            waveform_x = animation_left + (animation_width - waveform_total_width) / 2.0
            center_index = (NUM_BARS - 1) / 2.0

            if self._is_error:
                bar_color = (1.0, 0.28, 0.22)
                glow_color = (1.0, 0.22, 0.18)
            else:
                recording_color = (0.15, 0.85, 0.95)
                transcribing_color = (1.0, 0.70, 0.16)
                bar_color = tuple(
                    recording_color[i] * (1.0 - handoff) + transcribing_color[i] * handoff
                    for i in range(3)
                )
                glow_color = (1.0, 0.82, 0.24)

            for i in range(NUM_BARS):
                distance = abs(i - center_index) / center_index
                pulse = math.exp(-((distance - eased) / 0.16) ** 2)
                tail = max(0.0, 1.0 - abs(distance - eased) / 0.38)
                shimmer = 0.5 + 0.5 * math.sin(elapsed * 0.12 + i * 0.7)
                wave_target = 0.08 + pulse * 0.82 + tail * 0.12 + shimmer * 0.04
                flat_target = 0.10
                target = flat_target * (1.0 - settle) + wave_target * settle
                if self._completion_active:
                    center_pull = max(0.0, 1.0 - distance / 0.18)
                    unified_target = 0.02 + center_pull * 0.86
                    unify = _ease_in_out(min(1.0, completion_die / 0.55))
                    target = target * (1.0 - unify) + unified_target * unify
                    target *= 1.0 - completion_fade
                target = max(0.0, min(1.0, target))
                self._bar_targets[i] = target

            for i in range(NUM_BARS):
                diff = self._bar_targets[i] - self._bar_current[i]
                smoothing = 0.16 + handoff * 0.06
                if self._completion_active:
                    smoothing = 0.32 + completion_die * 0.42
                self._bar_current[i] += diff * smoothing

            for i in range(NUM_BARS):
                val = self._bar_current[i]
                bar_h = BAR_MIN_HEIGHT + val * (BAR_MAX_HEIGHT - BAR_MIN_HEIGHT)
                bar_x = waveform_x + i * (BAR_WIDTH + BAR_GAP)
                bar_y = animation_center_y - bar_h / 2.0

                if val > 0.28:
                    glow_rect = NSMakeRect(
                        bar_x - 1.0, bar_y - 1.0,
                        BAR_WIDTH + 2.0, bar_h + 2.0
                    )
                    glow_path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                        glow_rect, (BAR_WIDTH + 2.0) / 2, (BAR_WIDTH + 2.0) / 2
                    )
                    glow_alpha = (
                        (val - 0.28)
                        * (0.18 + handoff * 0.10)
                        * (1.0 - completion_fade)
                    )
                    NSColor.colorWithRed_green_blue_alpha_(
                        glow_color[0], glow_color[1], glow_color[2], glow_alpha
                    ).set()
                    glow_path.fill()

                bar_rect = NSMakeRect(bar_x, bar_y, BAR_WIDTH, bar_h)
                bar_path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                    bar_rect, BAR_WIDTH / 2, BAR_WIDTH / 2
                )
                brightness = 0.45 + val * 0.55
                alpha = (0.50 + val * 0.45) * (1.0 - completion_fade)
                NSColor.colorWithRed_green_blue_alpha_(
                    bar_color[0] * brightness,
                    bar_color[1] * brightness,
                    bar_color[2] * brightness,
                    alpha,
                ).set()
                bar_path.fill()

        content_top_y = layout_y + layout_height - ANIMATION_HEIGHT

        if self._message_text:
            # Fallback message strip. Normal recording/transcribing states stay visual-only.
            NSColor.colorWithRed_green_blue_alpha_(1.0, 1.0, 1.0, 0.06).set()
            sep_path = NSBezierPath.bezierPath()
            sep_path.moveToPoint_(NSMakePoint(STREAM_TEXT_MARGIN, content_top_y))
            sep_path.lineToPoint_(NSMakePoint(w - STREAM_TEXT_MARGIN, content_top_y))
            sep_path.setLineWidth_(0.5)
            sep_path.stroke()

            if self._is_error:
                text_color = NSColor.colorWithRed_green_blue_alpha_(1.0, 0.42, 0.36, 0.96)
            else:
                text_color = NSColor.colorWithRed_green_blue_alpha_(1.0, 1.0, 1.0, 0.72)
            message_attrs = {
                NSFontAttributeName: NSFont.systemFontOfSize_(11.0),
                NSForegroundColorAttributeName: text_color,
            }
            message = NSString.stringWithString_(self._message_text)
            message_size = message.sizeWithAttributes_(message_attrs)
            message_x = STREAM_TEXT_MARGIN
            message_y = content_top_y - MESSAGE_BAR_HEIGHT / 2.0 - message_size.height / 2.0
            message.drawAtPoint_withAttributes_(NSMakePoint(message_x, message_y), message_attrs)
            content_top_y -= MESSAGE_BAR_HEIGHT

        # --- Streaming transcript area (below status bar) ---
        if self._streaming_mode and (self._committed_text or self._partial_text):
            # Separator line below animation/message area
            sep_y = content_top_y
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


def _transcribe_wave_travel(elapsed):
    """Return the current center-out-center travel value for the yellow ping."""
    wave_elapsed = max(0, elapsed - TRANSCRIBE_SETTLE_FRAMES)
    raw_progress = (wave_elapsed * 0.026) % 2.0
    return raw_progress if raw_progress <= 1.0 else 2.0 - raw_progress


def _ease_in_out(progress):
    progress = max(0.0, min(1.0, progress))
    return 0.5 - 0.5 * math.cos(progress * math.pi)


def _zap_progress(progress):
    progress = max(0.0, min(1.0, progress))
    return 1.0 - (1.0 - progress) ** 3


def _zap_height_progress(progress):
    progress = max(0.0, min(1.0, progress))
    return _zap_progress(max(0.0, (progress - 0.08) / 0.92))


def _calc_overlay_height(message_text="", committed_text="", partial_text=""):
    """Calculate the panel height needed for optional fallback text."""
    height = OVERLAY_HEIGHT
    if message_text:
        height += MESSAGE_BAR_HEIGHT

    total_lines = 0
    if committed_text:
        total_lines += len(_wrap_text(committed_text, CHARS_PER_LINE))
    if partial_text:
        total_lines += len(_wrap_text(partial_text, CHARS_PER_LINE))

    if total_lines:
        height += total_lines * STREAM_LINE_HEIGHT + 8 + STREAM_TEXT_PADDING_BOTTOM

    height = min(height, STREAM_MAX_HEIGHT)
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

            # Resize only when optional fallback text/transcript needs room.
            if (
                not ov._content_view._opening_active
                and not ov._content_view._completion_active
                and (
                    ov._content_view._streaming_mode
                    or ov._content_view._message_text
                    or ov._current_height != OVERLAY_HEIGHT
                )
            ):
                ov._resize_to_fit()

            if ov._content_view._opening_active:
                ov._update_opening_frame()

            if ov._content_view._completion_active:
                ov._update_completion_frame()

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


class _OverlayCompleteTarget(NSObject):
    """ObjC target for starting the success close animation on main thread."""

    def initWithOverlay_(self, overlay):
        self = objc.super(_OverlayCompleteTarget, self).init()
        if self is None:
            return None
        self._overlay = overlay
        return self

    def doComplete_(self, sender):
        self._overlay._do_complete_and_hide()


class RecordingOverlay:
    """Floating HUD that shows recording state and audio level."""

    def __init__(self, recorder):
        self._recorder = recorder
        self._timer = None
        self._panel = None
        self._content_view = None
        self._current_height = OVERLAY_HEIGHT
        self._current_width = OVERLAY_WIDTH
        self._timer_target = _OverlayTimerTarget.alloc().initWithOverlay_(self)
        self._hide_target = _OverlayHideTarget.alloc().initWithOverlay_(self)
        self._complete_target = _OverlayCompleteTarget.alloc().initWithOverlay_(self)
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

    def _position_panel(self, height=None, width=None):
        """Position the panel centered above the dock. Grows upward."""
        screen = NSScreen.mainScreen()
        if screen is None:
            return
        if height is None:
            height = self._current_height
        if width is None:
            width = self._current_width
        visible = screen.visibleFrame()
        x = visible.origin.x + (visible.size.width - width) / 2
        base_y = visible.origin.y + 20
        y = base_y
        if height <= OVERLAY_HEIGHT:
            y += (OVERLAY_HEIGHT - height) / 2.0
        self._panel.setFrame_display_(
            NSMakeRect(x, y, width, height), True
        )
        self._content_view.setFrameSize_(NSMakeSize(width, height))
        self._current_height = height
        self._current_width = width

    def _update_completion_frame(self):
        cv = self._content_view
        if not cv._completion_active or cv._completion_start_tick is None:
            return
        elapsed = max(0, cv._tick - cv._completion_start_tick)
        curtain_elapsed = max(0, elapsed - CLOSE_RETURN_FRAMES - CLOSE_DIE_FRAMES)
        progress = min(1.0, curtain_elapsed / CLOSE_CURTAIN_FRAMES)
        field = 1.0 - _zap_progress(progress)
        height_field = 1.0 - _zap_height_progress(progress)
        width = max(2.0, OVERLAY_WIDTH * field)
        height = max(ZAP_MIN_HEIGHT, OVERLAY_HEIGHT * height_field)
        self._position_panel(height, width)
        if progress >= 1.0:
            self._do_hide()

    def _update_opening_frame(self):
        cv = self._content_view
        if not cv._opening_active or cv._opening_start_tick is None:
            return
        elapsed = max(0, cv._tick - cv._opening_start_tick)
        progress = min(1.0, elapsed / OPEN_ANIMATION_FRAMES)
        width = max(2.0, OVERLAY_WIDTH * _zap_progress(progress))
        height = max(ZAP_MIN_HEIGHT, OVERLAY_HEIGHT * _zap_height_progress(progress))
        self._position_panel(height, width)
        if progress >= 1.0:
            cv._opening_active = False
            cv._opening_start_tick = None
            self._position_panel(OVERLAY_HEIGHT, OVERLAY_WIDTH)

    def _resize_to_fit(self):
        """Resize the panel to fit the current transcript text. Grows upward."""
        cv = self._content_view
        needed = _calc_overlay_height(
            cv._message_text,
            cv._committed_text if cv._streaming_mode else "",
            cv._partial_text if cv._streaming_mode else "",
        )
        # Only resize if height changed meaningfully (avoid constant jitter)
        if abs(needed - self._current_height) >= STREAM_LINE_HEIGHT * 0.5:
            self._position_panel(needed)

    def show(self, status="Recording\u2026", streaming=False):
        self._content_view._message_text = ""
        self._content_view._is_recording = True
        self._content_view._is_error = False
        self._content_view._streaming_mode = streaming
        self._content_view._committed_text = ""
        self._content_view._partial_text = ""
        self._content_view._pulse_phase = 0.0
        self._content_view._rms_level = 0.0
        self._content_view._tick = 0
        self._content_view._opening_start_tick = 0
        self._content_view._opening_active = True
        self._content_view._transcribe_start_tick = 0
        self._content_view._completion_start_tick = None
        self._content_view._completion_start_travel = 0.0
        self._content_view._completion_active = False
        # Reset bar state for fresh animation
        self._content_view._bar_current = [0.0] * NUM_BARS
        self._content_view._bar_targets = [0.0] * NUM_BARS
        self._content_view._bar_phase = [random.uniform(0, math.tau) for _ in range(NUM_BARS)]
        self._content_view._bar_speed = [random.uniform(0.6, 1.4) for _ in range(NUM_BARS)]

        self._position_panel(ZAP_MIN_HEIGHT, 2.0)
        self._panel.orderFrontRegardless()
        self._start_timer()
        self._content_view.setNeedsDisplay_(True)
        logger.debug("Overlay shown: %s (streaming=%s)", status, streaming)

    def update_status(self, status):
        if self._content_view._is_recording:
            self._content_view._transcribe_start_tick = self._content_view._tick
        self._content_view._message_text = ""
        self._content_view._is_recording = False
        self._content_view._is_error = False
        self._content_view._opening_active = False
        self._content_view._opening_start_tick = None
        self._position_panel(OVERLAY_HEIGHT, OVERLAY_WIDTH)
        self._content_view.setNeedsDisplay_(True)
        logger.debug("Overlay status: %s", status)

    def complete_and_hide(self):
        """Play a short success animation, then hide. Safe from any thread."""
        self._complete_target.performSelectorOnMainThread_withObject_waitUntilDone_(
            "doComplete:", None, False
        )

    def _do_complete_and_hide(self):
        cv = self._content_view
        cv._message_text = ""
        cv._is_recording = False
        cv._is_error = False
        cv._streaming_mode = False
        cv._opening_active = False
        cv._opening_start_tick = None
        cv._completion_active = True
        cv._completion_start_tick = cv._tick
        elapsed = max(0, cv._tick - cv._transcribe_start_tick)
        cv._completion_start_travel = _transcribe_wave_travel(elapsed)
        self._position_panel(OVERLAY_HEIGHT, OVERLAY_WIDTH)
        self._panel.orderFrontRegardless()
        self._start_timer()
        cv.setNeedsDisplay_(True)

    def set_partial_text(self, text):
        """Update the streaming partial (interim) transcript text."""
        self._content_view._partial_text = text
        self._resize_to_fit()
        self._content_view.setNeedsDisplay_(True)

    def append_committed_text(self, text):
        """Append a committed transcript segment."""
        if self._content_view._committed_text:
            self._content_view._committed_text += " " + text
        else:
            self._content_view._committed_text = text
        # Clear partial since it's now committed
        self._content_view._partial_text = ""
        self._resize_to_fit()
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
        self._content_view._message_text = message
        self._content_view._is_recording = False
        self._content_view._is_error = True
        self._content_view._opening_active = False
        self._content_view._opening_start_tick = None
        self._position_panel(width=OVERLAY_WIDTH)
        self._resize_to_fit()
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
        self._content_view._opening_active = False
        self._content_view._opening_start_tick = None
        self._content_view._completion_active = False
        self._content_view._completion_start_tick = None
        self._content_view._completion_start_travel = 0.0
        self._current_height = OVERLAY_HEIGHT
        self._current_width = OVERLAY_WIDTH
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
