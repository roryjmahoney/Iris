#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
#
"""Render the README GIF from Iris's shipping dial model and painter.

No dial geometry or animation constants are duplicated here. Frames come from
``iris.gui.widgets.DialModel`` and are painted by ``tools/render_dial.py``, the
same headless path used to review the GTK widget's resolved primitives.
"""

from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

import cairo
from PIL import Image

REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY / "src"))

from iris.gui.widgets import DialModel, dial_palette_for  # noqa: E402
from render_dial import paint  # noqa: E402

SIDE = 288
FPS = 24
FRAME_MS = round(1000 / FPS)

IDLE_FRAMES = 24
SCANNING_FRAMES = 24
PROGRESS_FRAMES = 36
SUCCESS_FRAMES = 18
HOLD_FRAMES = 14


#: GitHub renders the README on either a near-white or a near-black ground, so
#: the hero image is produced twice and selected with <picture> + prefers-color-scheme.
LIGHT_BG = (0.984, 0.988, 0.992)
DARK_BG = (0.055, 0.055, 0.065)


def _paint_frame(model: DialModel, now_us: int, dark: bool = True) -> Image.Image:
    model.advance(now_us, reduced=False)
    frame = model.build_frame(
        SIDE,
        SIDE,
        now_us,
        dial_palette_for(dark=dark),
        reduced=False,
    )
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, SIDE, SIDE)
    paint(cairo.Context(surface), frame, SIDE, bg=DARK_BG if dark else LIGHT_BG)

    encoded = io.BytesIO()
    surface.write_to_png(encoded)
    encoded.seek(0)
    with Image.open(encoded) as image:
        return image.convert("RGB")


def build_frames(dark: bool = True) -> list[Image.Image]:
    """Build one calm idle-to-success loop from a synthetic monotonic clock."""
    total = (
        IDLE_FRAMES
        + SCANNING_FRAMES
        + PROGRESS_FRAMES
        + SUCCESS_FRAMES
        + HOLD_FRAMES
    )
    scanning_at = IDLE_FRAMES
    progress_at = scanning_at + SCANNING_FRAMES
    success_at = progress_at + PROGRESS_FRAMES

    epoch_us = 1_000_000
    model = DialModel(epoch_us)
    frames: list[Image.Image] = []

    for index in range(total):
        now_us = epoch_us + round(index * 1_000_000 / FPS)

        if index == scanning_at:
            model.enter(DialModel.STATE_SCANNING, now_us)
        elif progress_at <= index < success_at:
            step = index - progress_at
            fraction = step / max(PROGRESS_FRAMES - 1, 1)
            model.set_progress(fraction, now_us, animate=False)
        elif index == success_at:
            model.enter(DialModel.STATE_SUCCESS, now_us)

        frames.append(_paint_frame(dark=dark, model=model, now_us=now_us))

    return frames


def write_gif(output: Path, dark: bool = True) -> None:
    frames = build_frames(dark=dark)
    output.parent.mkdir(parents=True, exist_ok=True)

    paletted = [
        frame.quantize(
            colors=128,
            method=Image.Quantize.MEDIANCUT,
            dither=Image.Dither.FLOYDSTEINBERG,
        )
        for frame in frames
    ]
    first, *remaining = paletted
    first.save(
        output,
        format="GIF",
        save_all=True,
        append_images=remaining,
        duration=FRAME_MS,
        loop=0,
        disposal=2,
        optimize=False,
    )
    print(f"wrote {output} ({len(frames)} frames at {FPS} fps)")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Render docs/assets/iris-dial.gif from the real Iris DialModel."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="GIF destination (default: docs/assets/iris-dial[-light].gif)",
    )
    parser.add_argument(
        "--light",
        action="store_true",
        help="render the light-theme variant for GitHub's light colour scheme",
    )
    parser.add_argument(
        "--both",
        action="store_true",
        help="render both theme variants in one run",
    )
    args = parser.parse_args()

    assets = REPOSITORY / "docs" / "assets"
    if args.both:
        write_gif(assets / "iris-dial.gif", dark=True)
        write_gif(assets / "iris-dial-light.gif", dark=False)
        return 0

    default = assets / ("iris-dial-light.gif" if args.light else "iris-dial.gif")
    write_gif(args.output or default, dark=not args.light)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
