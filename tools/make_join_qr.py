#!/usr/bin/env python3
"""Generate the recruiting QR code for the public join page.

    pip install segno
    python tools/make_join_qr.py

Writes into docs/recruiting/. Re-run it if the URL ever changes — do not edit
the images by hand, and do not crop them (see "Quiet zone" below).

Why the outputs are what they are
---------------------------------
**SVG for print.** A QR code is pure geometry, so vector scales from a business
card to a banner with no resampling. Hand the SVG to anyone doing print work.

**PNG for screens.** Discord, slides, a phone photo of a monitor.

**Error correction H (30%).** The highest level. A recruiting code lives on
printed cards that get bent and scuffed, and stickers that peel — H keeps it
readable through damage covering up to ~30% of the symbol. It costs some data
density, which is free here: the URL is short.

**Dark-on-light only.** Scanners look for dark modules on a light background.
Inverted (light-on-dark) works on many modern phones but not all, and a failed
scan on a recruiting poster is a lost member. The branded variant below stays
dark-on-light and only tints *which* dark and *which* light, so it reads as Sol
Provision without gambling on the scan.

**Quiet zone.** The 4-module blank border is part of the spec, not padding.
Cropping it is the single most common way a QR code stops working, so it is
baked into every file here.
"""
import sys
from pathlib import Path

try:
    import segno
except ImportError:
    sys.exit("segno is required:  pip install segno")

JOIN_URL = "https://portal.solprovision.com/join"

OUT_DIR = Path(__file__).resolve().parent.parent / "docs" / "recruiting"

# Brand palette, from shared/brand/css/brand.css. ink-900 on bone keeps the
# contrast ratio well above what any scanner needs while still being ours.
INK_900 = "#0c0b0b"
BONE = "#F2EDE6"

# Border is in MODULES, not pixels — 4 is the spec minimum.
QUIET_ZONE = 4


def build():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    qr = segno.make(JOIN_URL, error="h")
    written = []

    # ── Plain: maximum compatibility. Use this one if in any doubt. ──
    svg = OUT_DIR / "join-qr.svg"
    qr.save(svg, scale=10, border=QUIET_ZONE, dark="#000000", light="#FFFFFF")
    written.append(svg)

    png = OUT_DIR / "join-qr.png"
    qr.save(png, scale=20, border=QUIET_ZONE, dark="#000000", light="#FFFFFF")
    written.append(png)

    # ── Branded: same geometry, Sol Provision ink on bone. ──
    svg_b = OUT_DIR / "join-qr-brand.svg"
    qr.save(svg_b, scale=10, border=QUIET_ZONE, dark=INK_900, light=BONE)
    written.append(svg_b)

    png_b = OUT_DIR / "join-qr-brand.png"
    qr.save(png_b, scale=20, border=QUIET_ZONE, dark=INK_900, light=BONE)
    written.append(png_b)

    print(f"URL      : {JOIN_URL}")
    print(f"Version  : {qr.version}  ({qr.symbol_size(border=0)[0]} modules square)")
    print(f"Error    : {qr.error.upper()} (~30% recoverable)")
    for p in written:
        print(f"  wrote  {p.relative_to(OUT_DIR.parent.parent)}  "
              f"({p.stat().st_size:,} bytes)")
    return written


if __name__ == "__main__":
    build()
