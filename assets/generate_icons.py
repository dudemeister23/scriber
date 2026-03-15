#!/usr/bin/env python3
"""Generate menubar icons and app icon for Scriber.

Creates:
- scriber/icons/mic_idle.pdf     (menubar icon, idle state)
- scriber/icons/mic_recording.pdf (menubar icon, recording state)
- assets/Scriber.icns            (app icon for the bundle)
"""

import os
import subprocess
import tempfile


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
ICONS_DIR = os.path.join(PROJECT_DIR, "scriber", "icons")
ICNS_PATH = os.path.join(SCRIPT_DIR, "Scriber.icns")


def menubar_svg(recording: bool) -> str:
    fill = "#FF3B30" if recording else "#000000"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<svg width="18" height="18" viewBox="0 0 18 18" xmlns="http://www.w3.org/2000/svg">
  <rect x="7" y="2" width="4" height="9" rx="2" fill="{fill}"/>
  <path d="M5 9 a4 4 0 0 0 8 0" stroke="{fill}" stroke-width="1.5" fill="none"/>
  <line x1="9" y1="13" x2="9" y2="15.5" stroke="{fill}" stroke-width="1.5"/>
  <line x1="7" y1="15.5" x2="11" y2="15.5" stroke="{fill}" stroke-width="1.5" stroke-linecap="round"/>
</svg>"""


def app_icon_svg() -> str:
    return """<?xml version="1.0" encoding="UTF-8"?>
<svg width="512" height="512" viewBox="0 0 512 512" xmlns="http://www.w3.org/2000/svg">
  <rect width="512" height="512" rx="100" fill="#1a1a2e"/>
  <rect x="210" y="100" width="92" height="200" rx="46" fill="#e94560"/>
  <path d="M160 260 a96 96 0 0 0 192 0" stroke="#e94560" stroke-width="20" fill="none"/>
  <line x1="256" y1="356" x2="256" y2="400" stroke="#e94560" stroke-width="20"/>
  <line x1="200" y1="400" x2="312" y2="400" stroke="#e94560" stroke-width="20" stroke-linecap="round"/>
</svg>"""


def svg_to_pdf(svg_str: str, pdf_path: str):
    """Convert SVG string to PDF using NSImage → PDF export."""
    from AppKit import NSImage, NSGraphicsContext
    from Foundation import NSURL, NSData, NSMakeRect, NSMakeSize

    data = NSData.dataWithBytes_length_(svg_str.encode(), len(svg_str.encode()))
    image = NSImage.alloc().initWithData_(data)
    if image is None:
        raise RuntimeError("Failed to load SVG into NSImage")

    size = image.size()
    rect = NSMakeRect(0, 0, size.width, size.height)

    image.lockFocus()
    rep = NSImage.alloc().initWithSize_(NSMakeSize(size.width, size.height))
    image.unlockFocus()

    # Write PDF using NSImage's built-in TIFF→PDF pipeline via a bitmap rep
    from AppKit import NSBitmapImageRep, NSPNGFileType

    # Simpler: just use image.TIFFRepresentation and sips
    tiff_data = image.TIFFRepresentation()
    with tempfile.NamedTemporaryFile(suffix=".tiff", delete=False) as f:
        tiff_data.writeToFile_atomically_(f.name, True)
        tiff_path = f.name

    try:
        subprocess.run(
            ["sips", "-s", "format", "pdf", tiff_path, "--out", pdf_path],
            check=True,
            capture_output=True,
        )
    finally:
        os.unlink(tiff_path)


def svg_to_png(svg_str: str, png_path: str, size: int):
    """Convert SVG string to PNG at given size."""
    from AppKit import NSImage, NSBitmapImageRep, NSGraphicsContext, NSPNGFileType
    from Foundation import NSData, NSMakeRect, NSMakeSize

    data = NSData.dataWithBytes_length_(svg_str.encode(), len(svg_str.encode()))
    image = NSImage.alloc().initWithData_(data)
    if image is None:
        raise RuntimeError("Failed to load SVG into NSImage")

    image.setSize_(NSMakeSize(size, size))

    rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, size, size, 8, 4, True, False, "NSCalibratedRGBColorSpace", 0, 0
    )
    rep.setSize_(NSMakeSize(size, size))

    ctx = NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.setCurrentContext_(ctx)
    image.drawInRect_fromRect_operation_fraction_(
        NSMakeRect(0, 0, size, size),
        NSMakeRect(0, 0, 0, 0),
        2,  # NSCompositingOperationSourceOver
        1.0,
    )
    NSGraphicsContext.restoreGraphicsState()

    png_data = rep.representationUsingType_properties_(NSPNGFileType, None)
    png_data.writeToFile_atomically_(png_path, True)


def create_icns(svg_str: str, icns_path: str):
    """Create .icns from SVG string."""
    with tempfile.TemporaryDirectory() as tmpdir:
        iconset = os.path.join(tmpdir, "Scriber.iconset")
        os.makedirs(iconset)

        for size in [16, 32, 64, 128, 256, 512]:
            svg_to_png(svg_str, os.path.join(iconset, f"icon_{size}x{size}.png"), size)
            svg_to_png(svg_str, os.path.join(iconset, f"icon_{size}x{size}@2x.png"), size * 2)

        subprocess.run(
            ["iconutil", "-c", "icns", iconset, "-o", icns_path],
            check=True,
            capture_output=True,
        )


def main():
    os.makedirs(ICONS_DIR, exist_ok=True)

    print("Generating menubar icons...")
    svg_to_pdf(menubar_svg(False), os.path.join(ICONS_DIR, "mic_idle.pdf"))
    svg_to_pdf(menubar_svg(True), os.path.join(ICONS_DIR, "mic_recording.pdf"))
    print(f"  -> {ICONS_DIR}/")

    print("Generating app icon...")
    create_icns(app_icon_svg(), ICNS_PATH)
    print(f"  -> {ICNS_PATH}")

    print("Done.")


if __name__ == "__main__":
    main()
