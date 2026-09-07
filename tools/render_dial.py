#!/usr/bin/env python3
"""Render the real dial geometry headlessly, for visual review.

This imports :class:`iris.gui.widgets.DialModel` and paints the exact
:class:`DialFrame` primitives the GTK widget paints, so what you see here is the
shipped geometry and timing -- not a reimplementation.  Cairo is used only as a
painter; the widget itself draws the same primitives with GSK, because
``python3-gi-cairo`` is absent and ``Gtk.Snapshot.append_cairo()`` cannot
marshal a context without it.

    python3 tools/render_dial.py            # contact sheet
    python3 tools/render_dial.py --reduced  # prefers-reduced-motion variant
"""
from __future__ import annotations

import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import cairo  # noqa: E402

from iris.gui.widgets import DialModel, dial_palette_for  # noqa: E402

SIDE = 240
BG = (0.055, 0.055, 0.065)


def paint(ctx: cairo.Context, frame, side: int, bg=None) -> None:
    """Paint one DialFrame. Order matters: glow under ticks, check on top."""
    ctx.set_source_rgb(*(bg if bg is not None else BG))
    ctx.paint()

    if frame.glow is not None:
        g = frame.glow
        grad = cairo.RadialGradient(g.cx, g.cy, 0.0, g.cx, g.cy, max(g.radius, 0.01))
        grad.add_color_stop_rgba(0.0, *g.rgba)
        grad.add_color_stop_rgba(1.0, g.rgba[0], g.rgba[1], g.rgba[2], 0.0)
        ctx.set_source(grad)
        ctx.arc(g.cx, g.cy, max(g.radius, 0.01), 0, 2 * math.pi)
        ctx.fill()

    ctx.set_line_cap(cairo.LINE_CAP_ROUND)
    ctx.set_line_width(frame.tick_width)
    for t in frame.ticks:
        ctx.set_source_rgba(*t.rgba)
        ctx.move_to(t.x0, t.y0)
        ctx.line_to(t.x1, t.y1)
        ctx.stroke()

    if frame.check is not None:
        c = frame.check
        ctx.set_line_cap(cairo.LINE_CAP_ROUND)
        ctx.set_line_join(cairo.LINE_JOIN_ROUND)
        ctx.set_line_width(c.width)
        ctx.set_source_rgba(*c.rgba)
        first, *rest = c.points
        ctx.move_to(*first)
        for p in rest:
            ctx.line_to(*p)
        ctx.stroke()


def shot(state: str, ms: float, progress: float | None, palette, reduced: bool):
    """Build one frame of *state* at *ms* into that state."""
    t0 = 1_000_000
    model = DialModel(t0)
    if state == "progress":
        model.set_progress(progress or 0.0, t0, animate=False)
    else:
        model.enter(state, t0)
    now = t0 + int(ms * 1000)
    model.advance(now, reduced)
    return model.build_frame(SIDE, SIDE, now, palette, reduced)


def main() -> int:
    reduced = "--reduced" in sys.argv
    dark = "--light" not in sys.argv
    palette = dial_palette_for(dark)

    shots = [
        ("idle 0ms", "idle", 0, None),
        ("idle 650ms", "idle", 650, None),
        ("scanning 400ms", "scanning", 400, None),
        ("progress 25%", "progress", 400, 0.25),
        ("progress 60%", "progress", 400, 0.60),
        ("progress 100%", "progress", 400, 1.0),
        ("success 150ms", "success", 150, None),
        ("success 400ms", "success", 400, None),
        ("success 720ms", "success", 720, None),
        ("failure 100ms", "failure", 100, None),
    ]

    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "frames")
    os.makedirs(out_dir, exist_ok=True)

    cols, rows = 5, 2
    sheet = cairo.ImageSurface(cairo.FORMAT_ARGB32, SIDE * cols, SIDE * rows)
    sc = cairo.Context(sheet)
    sc.set_source_rgb(0.03, 0.03, 0.04)
    sc.paint()

    for i, (label, state, ms, prog) in enumerate(shots):
        frame = shot(state, ms, prog, palette, reduced)
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, SIDE, SIDE)
        paint(cairo.Context(surf), frame, SIDE)

        name = label.replace(" ", "_").replace("%", "pct") + (".reduced" if reduced else "") + ".png"
        surf.write_to_png(os.path.join(out_dir, name))

        sc.set_source_surface(surf, (i % cols) * SIDE, (i // cols) * SIDE)
        sc.paint()
        sc.select_font_face("sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        sc.set_font_size(13)
        sc.set_source_rgba(1, 1, 1, 0.55)
        sc.move_to((i % cols) * SIDE + 10, (i // cols) * SIDE + SIDE - 10)
        sc.show_text(f"{label}  ({len(frame.ticks)} ticks)")

    sheet_name = "sheet.reduced.png" if reduced else "sheet.png"
    sheet.write_to_png(os.path.join(out_dir, sheet_name))
    print(f"wrote {os.path.join(out_dir, sheet_name)} and {len(shots)} individual frames")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
