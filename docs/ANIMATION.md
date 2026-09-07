<!-- SPDX-License-Identifier: AGPL-3.0-only -->

# Iris — Motion Specification

One visual language, implemented twice: GNOME Shell (GJS + Cairo, lock screen and
greeter) and GTK4 (Python + GskSnapshot/Cairo, enrolment and settings). Both must
look like the same product.

Heavily inspired by Apple's Face ID motion. Everything here is drawn from
primitives — arcs, ticks, strokes — not copied artwork.

## The core object: the dial

A ring built from **N = 48** short radial tick marks, evenly spaced, centred on
the camera preview (or standing alone on the lock screen).

    radius        R      = 0.42 x min(width, height)
    tick length   L      = 0.085 x R   (idle)  ->  0.16 x R  (peak)
    tick width    W      = max(1.5, 0.028 x R), round caps
    gap           preview circle sits at 0.86 x R

Ticks are indexed 0..N-1 clockwise from 12 o'clock.

## States

### 1. `idle` — waiting, no face yet
Ticks at base length, opacity 0.28. A **travelling wave** sweeps the ring:

    phase(t)   = (t / 2600ms) mod 1                     # one lap per 2.6 s
    d(i)       = wrapped distance from tick i to the wave head, in turns
    boost(i)   = exp(-(d(i) / 0.10)^2)                  # gaussian, ~10% of ring lit
    opacity(i) = 0.28 + 0.52 * boost(i)
    length(i)  = L * (1 + 0.55 * boost(i))

Calm and continuous. No hard edges, nothing strobing.

### 2. `scanning` — a face is present, matching in progress
Same wave, but faster (1600 ms/lap) and brighter (base 0.40). The ring also
breathes: overall scale 1.0 -> 1.015 -> 1.0 on a 1400 ms sine.

### 3. `progress` — enrolment only, 0..1
Ticks below the progress head are "filled": full opacity, length L * 1.5, accent
colour. Above the head they stay idle. The head itself gets a soft glow. The
fill fraction is tweened, never snapped, with `ease-out-cubic` over 260 ms.

### 4. `success`
Three beats, 720 ms total. This is the moment that has to feel good.

    0   -> 180ms   converge: every tick eases to full opacity and uniform
                   length L * 1.5; the wave stops. ease-out-cubic.
    120 -> 420ms   ring scale 1.0 -> 1.06 -> 1.0, overshoot spring
                   (ease-out-back, overshoot 1.7).
    260 -> 720ms   checkmark draws itself inside the ring, stroke length
                   0 -> 1 on ease-out-cubic. Ticks fade to 0.35 underneath
                   so the check reads clearly.

Checkmark geometry, normalised to the ring's inner box (unit square, origin
top-left), stroked with round caps and joins, width 0.075:

    (0.26, 0.52) -> (0.44, 0.70) -> (0.75, 0.32)

The first segment occupies the first 38% of the draw progress, the second the
remaining 62%, so the short leg does not look rushed.

### 5. `failure`
Not alarming. No red flash, no cross.

    0   -> 420ms   horizontal shake: x = A * sin(2*pi*3*u) * (1-u),
                   A = 0.035 * R, u = t/420ms  (3 oscillations, damped to zero)
    0   -> 300ms   ticks desaturate to the muted colour and return to base length

Then fall back to `idle` so the user can simply try again.

## Timing and easing

| Purpose | Duration | Easing |
|---|---|---|
| State cross-fade | 200 ms | ease-out-quad |
| Progress tween | 260 ms | ease-out-cubic |
| Success (total) | 720 ms | see beats |
| Failure shake | 420 ms | damped sine |
| Idle lap | 2600 ms | linear |
| Scanning lap | 1600 ms | linear |

Nothing exceeds 1 s, per the design brief. All motion is driven by a monotonic
clock and computed from elapsed time — never by counting frames — so it looks
identical at 60 Hz and 144 Hz and degrades gracefully if frames are dropped.

## Colour

Tokens, resolved per theme. The lock screen is always dark.

| Token | Dark | Light |
|---|---|---|
| `tick-idle` | rgba(255,255,255,0.28) | rgba(0,0,0,0.24) |
| `tick-active` | rgb(120,190,255) | rgb(0,122,255) |
| `tick-muted` | rgba(255,255,255,0.22) | rgba(0,0,0,0.20) |
| `accent` | rgb(120,190,255) | rgb(0,122,255) |
| `success` | rgb(52,199,89) | rgb(40,167,69) |

## Accessibility

`prefers-reduced-motion` (GTK: `Gtk.Settings:gtk-enable-animations`; Shell:
`org.gnome.desktop.interface enable-animations`) must be honoured. When motion is
reduced: no wave, no shake, no spring. States become static — idle is a dim even
ring, scanning is a brighter even ring, success draws the checkmark with a single
200 ms fade, failure dims once. The information conveyed must not depend on
motion, and every state must also be announced as text for screen readers.
