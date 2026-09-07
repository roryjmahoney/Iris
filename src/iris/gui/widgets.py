"""Reusable custom widgets for the Iris desktop app.

Five widgets, four shared disciplines.

:class:`FaceDial` is the implementation of ``docs/ANIMATION.md`` for the GTK4
side of the product.  Every number in it -- 48 ticks, the radius and length
ratios, the lap times, the success beats, the checkmark's normalised
coordinates -- comes from that document and nowhere else, because the GNOME
Shell lock-screen dial is built from the same numbers and the two have to look
like one product.  If a constant here disagrees with ANIMATION.md, ANIMATION.md
is right.

**Time, not frames.**  Every animation here is driven by
:meth:`Gtk.Widget.add_tick_callback` and reads elapsed time from the
:class:`Gdk.FrameClock`.  Counting frames instead would make each animation run
at a different speed on a 60 Hz panel, a 120 Hz panel, and a machine that
dropped three frames to a garbage collection -- and the enrolment dial, which
people watch closely, is exactly where that would show.  The travelling wave
goes further than "read the elapsed time each frame": its phase is
``((now - epoch) / lap) mod 1``, a pure function of the monotonic clock, so a
dropped frame cannot accumulate drift the way a per-frame delta would.
Durations are in milliseconds and none exceeds 720 ms: the interface should
feel answered, not narrated.  Tick callbacks are also transient -- each widget
registers one when an animation starts and removes it the instant the animation
settles, and drops it entirely while unmapped, so a window nobody is looking at
does not keep the compositor awake.

**GSK, not cairo.**  Every custom drawing here goes through
:class:`Gtk.Snapshot` and :class:`Gsk.PathBuilder` rather than
``snapshot.append_cairo()`` or ``Gtk.DrawingArea.set_draw_func``.  That is not a
stylistic preference and not a matter of taste about APIs: on this system
PyGObject has no cairo foreign-struct converter.  ``gi._gi_cairo`` ships in
``python3-gi-cairo``, which is not installed, so although ``import cairo``
succeeds, *marshalling* a ``cairo.Context`` across introspection does not --
``Gtk.Snapshot.append_cairo()`` raises ``TypeError: Couldn't find foreign struct
converter for 'cairo.Context'`` before a single drawing call runs, and the
widget renders blank.  Installing that package is the only fix and is out of
scope, so cairo cannot be used here.  Nothing is lost: :class:`Gsk.Stroke`
gives the round caps and joins the dial and the checkmark need, arcs come from
:meth:`Gsk.PathBuilder.svg_arc_to`, and the strokes are rasterised on the GPU
rather than on the CPU every frame.

**Reduced motion is a real mode, not a switch that stops the clock.**  When
``Gtk.Settings:gtk-enable-animations`` is off, the wave, the breathing, the
spring and the shake are gone, but every state still *looks* different from
every other and still says what it is in text -- see
:func:`animations_enabled` and the ``reduced`` branches below.

**Themes come from the platform, the dial's colours come from the spec.**
Nothing here hard-codes a background; general foreground comes from
:meth:`Gtk.Widget.get_color`, i.e. from CSS, and the badge's accent follows the
user's system accent through :class:`Adw.StyleManager`.  The dial is the
deliberate exception: its five tokens are pinned literals from ANIMATION.md,
mirrored in ``style.css`` as ``--iris-tick-*`` / ``--iris-dial-*``.  They are
pinned because the Shell extension that draws the same dial on the lock screen
cannot reach libadwaita's accent, and a dial that is blue in the greeter and
purple in the wizard is two products.  GTK4 offers no API to read a CSS custom
property back out of a widget, so the literals genuinely live in both files:
change one and change the other.
"""

from __future__ import annotations

import logging
import math
from typing import Callable, Final, NamedTuple, Sequence

from gi.repository import Adw, Gdk, GLib, Graphene, Gsk, Gtk

_LOG = logging.getLogger("iris.gui.widgets")

# -- motion -----------------------------------------------------------------

#: One sweep of the scanning shimmer.
SCAN_CYCLE_MS: Final[float] = 900.0

#: Fade for the shimmer appearing or disappearing.
SCAN_FADE_MS: Final[float] = 220.0

#: The badge draws its circle, then its glyph, back to back.
BADGE_CIRCLE_MS: Final[float] = 380.0
BADGE_GLYPH_MS: Final[float] = 300.0

#: Fallback accent, used only if libadwaita cannot report the system accent.
#: A deep blue that clears 4.5:1 against both surfaces in ``style.css``.
_FALLBACK_ACCENT: Final[tuple[float, float, float]] = (0.204, 0.400, 0.902)

_SUCCESS_LIGHT: Final[tuple[float, float, float]] = (0.106, 0.545, 0.310)
_SUCCESS_DARK: Final[tuple[float, float, float]] = (0.353, 0.804, 0.514)
_FAILURE_LIGHT: Final[tuple[float, float, float]] = (0.741, 0.180, 0.180)
_FAILURE_DARK: Final[tuple[float, float, float]] = (0.949, 0.463, 0.463)

#: Past this the progress arc is drawn as a closed circle instead.  An SVG arc
#: whose end point coincides with its start is degenerate and renders as
#: nothing at all, so the ring would blink out exactly as it completed.
_FULL_CIRCLE_EPSILON: Final[float] = 0.9995


# --------------------------------------------------------------------------
# the dial: every constant below is quoted from docs/ANIMATION.md
# --------------------------------------------------------------------------

#: "A ring built from N = 48 short radial tick marks."  48 divides into 2, 3,
#: 4, 6, 8 and 12, so the head of a progress fill lands exactly on a tick at
#: every fraction a user is likely to notice.
DIAL_TICKS: Final[int] = 48

#: ``R = 0.42 x min(width, height)``.
DIAL_RADIUS_RATIO: Final[float] = 0.42
#: ``L = 0.085 x R`` at rest, ...
DIAL_TICK_BASE_RATIO: Final[float] = 0.085
#: ... and ``0.16 x R`` at peak.  Nothing the states below compute reaches this
#: (the wave peaks at 1.55 L and a filled tick is 1.5 L); it is the envelope,
#: enforced as a clamp so no future state can silently break the ring's
#: silhouette.
DIAL_TICK_PEAK_RATIO: Final[float] = 0.16
#: ``W = max(1.5, 0.028 x R)``, round caps.  The floor keeps the ticks visible
#: rather than sub-pixel on a small dial in a compact window.
DIAL_TICK_WIDTH_RATIO: Final[float] = 0.028
DIAL_TICK_WIDTH_MIN: Final[float] = 1.5
#: "gap: preview circle sits at 0.86 x R".
DIAL_INNER_RATIO: Final[float] = 0.86

#: Longest length multiplier any tick may take, in units of L.
_DIAL_TICK_MAX_MULTIPLE: Final[float] = DIAL_TICK_PEAK_RATIO / DIAL_TICK_BASE_RATIO

#: One lap of the travelling wave: 2600 ms idle, 1600 ms scanning.
DIAL_IDLE_LAP_MS: Final[float] = 2600.0
DIAL_SCAN_LAP_MS: Final[float] = 1600.0

#: ``boost(i) = exp(-(d(i) / 0.10)^2)`` -- a gaussian about a tenth of the ring
#: wide, which is what makes the lit arc have no edge to strobe against.
DIAL_WAVE_SIGMA: Final[float] = 0.10
#: ``length(i) = L * (1 + 0.55 * boost(i))``.
DIAL_WAVE_LENGTH_BOOST: Final[float] = 0.55

#: "The ring also breathes: overall scale 1.0 -> 1.015 -> 1.0 on a 1400 ms sine."
DIAL_BREATHE_MS: Final[float] = 1400.0
DIAL_BREATHE_AMPLITUDE: Final[float] = 0.015

#: "The fill fraction is tweened, never snapped, with ease-out-cubic over 260 ms."
DIAL_PROGRESS_MS: Final[float] = 260.0
#: A filled tick is "full opacity, length L * 1.5, accent colour".
DIAL_FILL_LENGTH: Final[float] = 1.5
#: Radius of the head's glow, in units of L.  Wide enough to read as light
#: spilling off the leading tick, tight enough not to wash out its neighbours.
DIAL_HEAD_GLOW_MULTIPLE: Final[float] = 3.2
DIAL_HEAD_GLOW_ALPHA: Final[float] = 0.42

#: "State cross-fade | 200 ms | ease-out-quad", and the whole of the
#: reduced-motion success ("a single 200 ms fade").
DIAL_CROSSFADE_MS: Final[float] = 200.0

#: The three beats of success, 720 ms end to end.
DIAL_SUCCESS_MS: Final[float] = 720.0
DIAL_CONVERGE_MS: Final[float] = 180.0          # 0 -> 180
DIAL_SPRING_START_MS: Final[float] = 120.0      # 120 -> 420
DIAL_SPRING_MS: Final[float] = 300.0
DIAL_SPRING_SCALE: Final[float] = 0.06          # 1.0 -> 1.06 -> 1.0
#: Fraction of the spring beat spent swelling, the rest settling back.  The
#: spec writes the beat as "1.0 -> 1.06 -> 1.0", which reads as symmetric, and
#: the Shell extension's SPRING_RISE has to carry the same number or the pop
#: peaks 30 ms apart in the two runtimes -- close enough to look like a bug and
#: far enough to be seen side by side.
DIAL_SPRING_RISE: Final[float] = 0.5
DIAL_CHECK_START_MS: Final[float] = 260.0       # 260 -> 720
DIAL_CHECK_MS: Final[float] = 460.0
#: "Ticks fade to 0.35 underneath so the check reads clearly."
DIAL_SUCCESS_TICK_ALPHA: Final[float] = 0.35

#: The checkmark, normalised to the ring's inner box (unit square, origin top
#: left), stroked with round caps and joins.
DIAL_CHECK_POINTS: Final[tuple[tuple[float, float], ...]] = (
    (0.26, 0.52),
    (0.44, 0.70),
    (0.75, 0.32),
)
DIAL_CHECK_WIDTH: Final[float] = 0.075
#: "The first segment occupies the first 38% of the draw progress, the second
#: the remaining 62%, so the short leg does not look rushed."
DIAL_CHECK_FIRST_LEG: Final[float] = 0.38

#: Failure: ``x = A * sin(2*pi*3*u) * (1-u)``, ``A = 0.035 * R``, over 420 ms,
#: with the ticks settling to the muted colour over the first 300 ms.
DIAL_SHAKE_MS: Final[float] = 420.0
DIAL_SHAKE_CYCLES: Final[float] = 3.0
DIAL_SHAKE_AMPLITUDE: Final[float] = 0.035
DIAL_SETTLE_MS: Final[float] = 300.0


# -- dial colour tokens ------------------------------------------------------
#
# ANIMATION.md's table, verbatim, and mirrored in style.css as --iris-tick-* /
# --iris-dial-*.  See the module docstring for why they are literals here
# instead of being read back out of CSS.

#: ``tick-idle`` / ``tick-muted`` are the same hue at two opacities, so only the
#: hue is stored; the opacities live in :class:`_DialPalette`.
_DIAL_TICK_DARK: Final[tuple[float, float, float]] = (1.0, 1.0, 1.0)
_DIAL_TICK_LIGHT: Final[tuple[float, float, float]] = (0.0, 0.0, 0.0)

_DIAL_IDLE_ALPHA_DARK: Final[float] = 0.28
_DIAL_IDLE_ALPHA_LIGHT: Final[float] = 0.24
_DIAL_MUTED_ALPHA_DARK: Final[float] = 0.22
_DIAL_MUTED_ALPHA_LIGHT: Final[float] = 0.20

#: ``accent`` and ``tick-active`` are the same colour in both themes, so the
#: dial keeps one accent name.
_DIAL_ACCENT_DARK: Final[tuple[float, float, float]] = (120 / 255, 190 / 255, 1.0)
_DIAL_ACCENT_LIGHT: Final[tuple[float, float, float]] = (0.0, 122 / 255, 1.0)
_DIAL_SUCCESS_DARK: Final[tuple[float, float, float]] = (52 / 255, 199 / 255, 89 / 255)
_DIAL_SUCCESS_LIGHT: Final[tuple[float, float, float]] = (40 / 255, 167 / 255, 69 / 255)

#: The spec writes its opacity arithmetic for the dark ring -- "opacity(i) =
#: 0.28 + 0.52 * boost(i)", "brighter (base 0.40)" -- because the lock screen is
#: always dark, and gives light only as a separate resting token (0.24).  Light
#: therefore keeps the dark *ratios* rather than the dark numbers: every
#: opacity is scaled by 0.24/0.28, so the wave has the same visual weight
#: against white that it has against black instead of being 17% too heavy.
_DIAL_BOOST_ALPHA_DARK: Final[float] = 0.52
_DIAL_SCAN_ALPHA_DARK: Final[float] = 0.40
_DIAL_LIGHT_ALPHA_SCALE: Final[float] = _DIAL_IDLE_ALPHA_LIGHT / _DIAL_IDLE_ALPHA_DARK


# --------------------------------------------------------------------------
# easing
# --------------------------------------------------------------------------

def ease_out_cubic(t: float) -> float:
    """Decelerating curve: fast start, soft landing.

    The default for anything moving *to* a value the user is waiting on -- it
    covers most of the distance immediately, so the change registers, then
    settles without the bounce that reads as decoration.
    """
    t = min(1.0, max(0.0, t))
    return 1.0 - (1.0 - t) ** 3


def ease_out_quad(t: float) -> float:
    """A gentler deceleration than cubic.

    ANIMATION.md's curve for state cross-fades.  Quadratic rather than cubic
    because a cross-fade has no destination the eye is tracking: the softer
    curve spends more of its 200 ms in the middle of the blend, where the
    change is legible, instead of front-loading it.
    """
    t = min(1.0, max(0.0, t))
    return 1.0 - (1.0 - t) ** 2


def ease_out_back(t: float, overshoot: float = 1.7) -> float:
    """Decelerating curve that overshoots 1 and springs back to it.

    The standard formulation, with *overshoot* being the ``c1`` of the usual
    ``easeOutBack``; ANIMATION.md pins it at 1.7, which peaks about 10% past
    the target.  Used for the success ring's pop, and nothing else -- a spring
    on a control the user is aiming at makes it harder to hit.
    """
    t = min(1.0, max(0.0, t))
    c1 = overshoot
    c3 = c1 + 1.0
    return 1.0 + c3 * (t - 1.0) ** 3 + c1 * (t - 1.0) ** 2


def damped_sine(u: float, cycles: float = DIAL_SHAKE_CYCLES) -> float:
    """``sin(2*pi*cycles*u) * (1-u)`` -- ANIMATION.md's failure shake.

    Returns a *signed* displacement in the range (-1, 1) that starts at zero,
    oscillates *cycles* times and is damped linearly to exactly zero at
    ``u == 1``.  Linear damping rather than exponential is deliberate: an
    exponential tail never quite reaches zero, and a ring that is still
    trembling by a third of a pixel when the next state begins reads as a
    rendering fault.
    """
    u = min(1.0, max(0.0, u))
    return math.sin(2.0 * math.pi * cycles * u) * (1.0 - u)


def ease_in_out_sine(t: float) -> float:
    """Symmetric curve for looping motion, with no visible seam at the wrap."""
    t = min(1.0, max(0.0, t))
    return 0.5 - 0.5 * math.cos(math.pi * t)


def _clamp01(value: float) -> float:
    return min(1.0, max(0.0, value))


def _lerp(a: float, b: float, t: float) -> float:
    """Linear blend.  *t* is not clamped here because every caller passes a
    value that has already been through an easing function, all of which
    clamp; clamping twice would only hide a caller that had not."""
    return a + (b - a) * t


# --------------------------------------------------------------------------
# reduced motion
# --------------------------------------------------------------------------

def animations_enabled() -> bool:
    """Whether the desktop wants animation at all.

    ``gtk-enable-animations`` is GTK's rendering of the platform's
    reduced-motion preference (on GNOME, ``org.gnome.desktop.interface
    enable-animations``), so reading it here is how a widget honours a setting
    made in Accessibility.  Absent settings -- no display connection, a unit
    test -- are treated as "animation is fine", because the alternative is
    that a misconfigured environment silently ships the degraded experience.
    """
    settings = Gtk.Settings.get_default()
    if settings is None:  # pragma: no cover - only without a display
        return True
    return bool(settings.get_property("gtk-enable-animations"))


# --------------------------------------------------------------------------
# colour
# --------------------------------------------------------------------------

def is_dark() -> bool:
    """True when libadwaita is currently rendering the dark variant."""
    return Adw.StyleManager.get_default().get_dark()


def accent_rgba() -> Gdk.RGBA:
    """The system accent colour, adjusted for legibility as a foreground.

    ``get_accent_color_rgba`` returns the colour intended as a *background*
    (with white text on it).  A 6 px arc in that colour is fine on a light
    surface but too dim on a dark one, which is what ``to_standalone_rgba``
    exists to correct.
    """
    manager = Adw.StyleManager.get_default()
    try:
        return Adw.AccentColor.to_standalone_rgba(
            manager.get_accent_color(), manager.get_dark()
        )
    except Exception as exc:  # noqa: BLE001 - older or unusual libadwaita
        _LOG.debug("falling back to the built-in accent colour: %s", exc)
        return _rgb(_FALLBACK_ACCENT)


def _rgb(triple: tuple[float, float, float], alpha: float = 1.0) -> Gdk.RGBA:
    rgba = Gdk.RGBA()
    rgba.red, rgba.green, rgba.blue = triple
    rgba.alpha = alpha
    return rgba


def with_alpha(source: Gdk.RGBA, alpha: float) -> Gdk.RGBA:
    """A copy of *source* at a different opacity."""
    rgba = Gdk.RGBA()
    rgba.red, rgba.green, rgba.blue = source.red, source.green, source.blue
    rgba.alpha = alpha
    return rgba


def _lerp_rgb(
    a: tuple[float, float, float], b: tuple[float, float, float], t: float
) -> tuple[float, float, float]:
    t = _clamp01(t)
    return (
        a[0] + (b[0] - a[0]) * t,
        a[1] + (b[1] - a[1]) * t,
        a[2] + (b[2] - a[2]) * t,
    )


class _DialPalette(NamedTuple):
    """ANIMATION.md's colour table, resolved for one theme.

    Resolved per snapshot rather than cached on the widget: libadwaita can
    switch the colour scheme mid-animation (the desktop's night mode, a
    portal), and a dial that finished its success beat in yesterday's palette
    would be the only stale thing on screen.
    """

    tick: tuple[float, float, float]
    idle_alpha: float
    scan_alpha: float
    muted_alpha: float
    boost_alpha: float
    accent: tuple[float, float, float]
    success: tuple[float, float, float]


def dial_palette() -> _DialPalette:
    """The dial's five tokens for the theme libadwaita is rendering now."""
    return dial_palette_for(is_dark())


def dial_palette_for(dark: bool) -> _DialPalette:
    """The dial's five tokens for an explicitly named theme.

    Split out from :func:`dial_palette` so the offline renderer in
    ``tools/render_dial.py`` can ask for the dark column -- the lock screen's
    column, the one the reference frames have to show -- without an
    :class:`Adw.StyleManager`, which needs a display connection to answer.
    """
    if dark:
        return _DialPalette(
            tick=_DIAL_TICK_DARK,
            idle_alpha=_DIAL_IDLE_ALPHA_DARK,
            scan_alpha=_DIAL_SCAN_ALPHA_DARK,
            muted_alpha=_DIAL_MUTED_ALPHA_DARK,
            boost_alpha=_DIAL_BOOST_ALPHA_DARK,
            accent=_DIAL_ACCENT_DARK,
            success=_DIAL_SUCCESS_DARK,
        )
    # See _DIAL_LIGHT_ALPHA_SCALE: the spec's opacity arithmetic is written for
    # the dark ring, and light preserves its ratios rather than its numbers.
    return _DialPalette(
        tick=_DIAL_TICK_LIGHT,
        idle_alpha=_DIAL_IDLE_ALPHA_LIGHT,
        scan_alpha=_DIAL_SCAN_ALPHA_DARK * _DIAL_LIGHT_ALPHA_SCALE,
        muted_alpha=_DIAL_MUTED_ALPHA_LIGHT,
        boost_alpha=_DIAL_BOOST_ALPHA_DARK * _DIAL_LIGHT_ALPHA_SCALE,
        accent=_DIAL_ACCENT_LIGHT,
        success=_DIAL_SUCCESS_LIGHT,
    )


def success_rgba() -> Gdk.RGBA:
    return _rgb(_SUCCESS_DARK if is_dark() else _SUCCESS_LIGHT)


def failure_rgba() -> Gdk.RGBA:
    return _rgb(_FAILURE_DARK if is_dark() else _FAILURE_LIGHT)


def _stop(offset: float, colour: Gdk.RGBA) -> Gsk.ColorStop:
    stop = Gsk.ColorStop()
    stop.offset = offset
    stop.color = colour
    return stop


def _point(x: float, y: float) -> Graphene.Point:
    return Graphene.Point().init(x, y)


# --------------------------------------------------------------------------
# animation plumbing
# --------------------------------------------------------------------------

class _Tween:
    """A single interpolation between two numbers, in wall-clock time.

    Holds no timer of its own: the owning widget pumps it from its tick
    callback with the frame clock's timestamp, so every animation in a window
    advances against the same clock and stays in step.
    """

    __slots__ = ("_from", "_to", "_start_us", "_duration_us", "_easing", "_active")

    def __init__(self, value: float = 0.0) -> None:
        self._from = value
        self._to = value
        self._start_us = 0
        self._duration_us = 0
        self._easing: Callable[[float], float] = ease_out_cubic
        self._active = False

    @property
    def active(self) -> bool:
        return self._active

    @property
    def target(self) -> float:
        return self._to

    def set(self, value: float) -> None:
        """Jump straight to *value*, cancelling any animation in flight."""
        self._from = self._to = value
        self._active = False

    def animate(
        self,
        value: float,
        now_us: int,
        duration_ms: float,
        easing: Callable[[float], float] = ease_out_cubic,
    ) -> None:
        """Retarget to *value*, starting from wherever the tween is right now."""
        current = self.value(now_us)
        if duration_ms <= 0 or abs(value - current) < 1e-4:
            self.set(value)
            return
        self._from = current
        self._to = value
        self._start_us = now_us
        self._duration_us = int(duration_ms * 1000)
        self._easing = easing
        self._active = True

    def value(self, now_us: int) -> float:
        """The interpolated value at *now_us*, in frame-clock microseconds."""
        if not self._active:
            return self._to
        elapsed = now_us - self._start_us
        if elapsed >= self._duration_us:
            self._active = False
            self._from = self._to
            return self._to
        return self._from + (self._to - self._from) * self._easing(
            elapsed / self._duration_us
        )


class _Canvas(Gtk.Widget):
    """A widget that draws itself, with a settable natural size.

    ``Gtk.DrawingArea`` would normally fill this role, but its only drawing
    entry point is a cairo callback that cannot run here (see the module
    docstring).  This provides the same ``set_content_width``/
    ``set_content_height`` interface so call sites read the same, and leaves
    ``do_snapshot`` to subclasses.
    """

    #: Floor for the natural size, so a narrow window can always shrink the
    #: widget rather than being forced wider by it.
    MIN_SIZE: Final[int] = 96

    def __init__(self, content_width: int = 0, content_height: int = 0, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._content_width = int(content_width)
        self._content_height = int(content_height)

    def set_content_width(self, width: int) -> None:
        if int(width) != self._content_width:
            self._content_width = int(width)
            self.queue_resize()

    def set_content_height(self, height: int) -> None:
        if int(height) != self._content_height:
            self._content_height = int(height)
            self.queue_resize()

    def do_measure(self, orientation: Gtk.Orientation, for_size: int) -> tuple[int, int, int, int]:
        del for_size
        natural = (
            self._content_width
            if orientation == Gtk.Orientation.HORIZONTAL
            else self._content_height
        )
        return (min(self.MIN_SIZE, natural), natural, -1, -1)

    def _now_us(self) -> int:
        """Current frame time, falling back to the monotonic clock.

        An unrealised widget has no frame clock, but callers still set values
        on it during construction; those need a timestamp on the same
        timescale, which :func:`GLib.get_monotonic_time` is.
        """
        clock = self.get_frame_clock()
        return clock.get_frame_time() if clock is not None else GLib.get_monotonic_time()


class _Animated:
    """Mixin adding an on-demand tick callback to a :class:`_Canvas`.

    Subclasses implement :meth:`_advance`, which returns ``True`` while it
    still needs frames.  The callback is torn down as soon as it returns
    ``False``, and again whenever the widget leaves the screen.

    Two separate questions are tracked, because conflating them is what makes
    animations either die or leak.  :attr:`_wants_frames` is whether the
    *animation* still has somewhere to go; ``_tick_id`` is whether the widget
    is currently *able* to get frames, which it is not while unmapped.  Keeping
    the first across a hide is what lets a running animation pick itself back
    up when the user returns to the page instead of freezing on the last frame
    it managed to draw.
    """

    _tick_id: int = 0
    _wants_frames: bool = False
    _anim_settings: Gtk.Settings | None = None
    _anim_settings_id: int = 0

    def _connect_visibility(self) -> None:
        """Tie the tick callback to the widget actually being on screen.

        Called from each subclass's constructor.  GTK delivers no frames to an
        unmapped widget, so a callback held across a hide is inert; the point
        of dropping it is that the widget stops being a reason for the frame
        clock to keep running at all.
        """
        widget: Gtk.Widget = self  # type: ignore[assignment]
        widget.connect("map", lambda _w: self._resume_ticking())
        widget.connect("unmap", lambda _w: self._suspend_ticking())

        # Reduced motion is not only a paint-time concern.  Every _advance()
        # below asks animations_enabled() whether it still needs frames, so a
        # widget that settled *while* motion was reduced has already dropped
        # its tick callback -- and turning animation back on in Accessibility
        # would leave a permanently frozen dial until something happened to
        # change its state.  The GNOME Shell half of the dial watches
        # `changed::enable-animations` for exactly this reason; this is the
        # GTK equivalent, and without it the two runtimes disagree about what
        # "reachable" means.
        #
        # This handler is the one signal in this file that has to be
        # disconnected by hand: Gtk.Settings is a per-display singleton that
        # long outlives any widget connected to it, so leaving the connection
        # in place would keep every dial the app ever built alive for the
        # lifetime of the process.
        settings = Gtk.Settings.get_default()
        if settings is not None:  # pragma: no branch - only None without a display
            self._anim_settings = settings
            self._anim_settings_id = settings.connect(
                "notify::gtk-enable-animations", self._on_animations_changed
            )
            widget.connect("destroy", lambda _w: self._disconnect_animations())

    def _on_animations_changed(self, *_args: object) -> None:
        """Ask for one frame and let the widget's own _advance() decide.

        Deliberately not "start animating": _advance() is the single place that
        knows whether this widget still has anywhere to go, and a poke that is
        wrong simply costs one frame before the callback removes itself again.
        """
        self._start_ticking()
        self.queue_draw()  # type: ignore[attr-defined]

    def _disconnect_animations(self) -> None:
        if self._anim_settings_id and self._anim_settings is not None:
            self._anim_settings.disconnect(self._anim_settings_id)
        self._anim_settings_id = 0
        self._anim_settings = None

    def _start_ticking(self) -> None:
        self._wants_frames = True
        self._resume_ticking()

    def _stop_ticking(self) -> None:
        self._wants_frames = False
        self._suspend_ticking()

    def _resume_ticking(self) -> None:
        if self._tick_id or not self._wants_frames:
            return
        if not self.get_mapped():  # type: ignore[attr-defined]
            return  # nothing to draw on; the map handler will come back
        self._tick_id = self.add_tick_callback(self._on_tick)  # type: ignore[attr-defined]

    def _suspend_ticking(self) -> None:
        """Give up the frame clock but remember that the animation wants it."""
        if not self._tick_id:
            return
        self.remove_tick_callback(self._tick_id)  # type: ignore[attr-defined]
        self._tick_id = 0

    def _on_tick(self, _widget: Gtk.Widget, clock: Gdk.FrameClock) -> bool:
        keep_going = self._advance(clock.get_frame_time())
        self.queue_draw()  # type: ignore[attr-defined]
        if not keep_going:
            self._tick_id = 0
            self._wants_frames = False
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def _advance(self, now_us: int) -> bool:  # pragma: no cover - overridden
        raise NotImplementedError


# --------------------------------------------------------------------------
# path helpers
# --------------------------------------------------------------------------

def _stroke(width: float) -> Gsk.Stroke:
    """A round-capped, round-joined stroke.

    Round ends are what stop a partly-filled ring from looking like a broken
    one, and they keep the two segments of the success tick reading as a single
    pen movement rather than two sticks.
    """
    stroke = Gsk.Stroke.new(width)
    stroke.set_line_cap(Gsk.LineCap.ROUND)
    stroke.set_line_join(Gsk.LineJoin.ROUND)
    return stroke


def _arc_path(cx: float, cy: float, radius: float, fraction: float) -> Gsk.Path | None:
    """An arc of *fraction* of a circle, starting at twelve o'clock, clockwise.

    Returns ``None`` for a zero-length arc: an empty path still costs a render
    node, and a zero-length round-capped stroke would draw a stray dot at the
    top of the ring before any progress had been made.
    """
    if fraction <= 0.0 or radius <= 0.0:
        return None

    builder = Gsk.PathBuilder.new()
    if fraction >= _FULL_CIRCLE_EPSILON:
        builder.add_circle(_point(cx, cy), radius)
        return builder.to_path()

    angle = 2.0 * math.pi * fraction
    builder.move_to(cx, cy - radius)
    # Y grows downwards, so the SVG "positive sweep" direction is clockwise --
    # the direction every progress dial anyone has used goes.
    builder.svg_arc_to(
        radius, radius,
        0.0,                    # no rotation: the radii are equal
        angle > math.pi,        # large-arc flag
        True,                   # positive (clockwise) sweep
        cx + radius * math.sin(angle),
        cy - radius * math.cos(angle),
    )
    return builder.to_path()


def _polyline(points: Sequence[tuple[float, float]]) -> Gsk.Path:
    builder = Gsk.PathBuilder.new()
    builder.move_to(*points[0])
    for point in points[1:]:
        builder.line_to(*point)
    return builder.to_path()


def _lerp_point(
    a: tuple[float, float], b: tuple[float, float], t: float
) -> tuple[float, float]:
    t = min(1.0, max(0.0, t))
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)


# --------------------------------------------------------------------------
# CameraPreview
# --------------------------------------------------------------------------

class CameraPreview(Gtk.Widget):
    """A live camera feed inside a circular mask.

    Implemented with a custom ``snapshot`` rather than a :class:`Gtk.Picture`
    in a styled container, because GTK4's CSS ``border-radius`` shapes a
    widget's own background and border but does not clip a child's contents --
    the square corners of the video would still be drawn.  Pushing a
    :class:`Gsk.RoundedRect` clip is the only way to get a genuinely round
    frame, and it costs one node.

    The frame is drawn *cover*-fit: scaled until it fills the circle and
    centre-cropped, so a 16:9 sensor produces a full circle with no letterbox
    bars.  That is what makes the mask read as a portal rather than a mask.
    """

    __gtype_name__ = "IrisCameraPreview"

    def __init__(self, natural_size: int = 260, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._texture: Gdk.Texture | None = None
        self._natural = int(natural_size)
        self._dim = 0.0
        self._show_ring = True

        self.set_overflow(Gtk.Overflow.HIDDEN)
        self.set_accessible_role(Gtk.AccessibleRole.IMG)
        self.update_property([Gtk.AccessibleProperty.LABEL], ["Camera preview"])
        self.update_property(
            [Gtk.AccessibleProperty.DESCRIPTION],
            ["A live picture from the infrared camera. Nothing is recorded."],
        )

    # -- content -----------------------------------------------------------

    def set_texture(self, texture: Gdk.Texture | None) -> None:
        """Show *texture*, or clear the preview when it is ``None``."""
        self._texture = texture
        self.queue_draw()

    @property
    def has_image(self) -> bool:
        return self._texture is not None

    def clear(self) -> None:
        self.set_texture(None)

    def set_dim(self, dim: float) -> None:
        """Dim the image by *dim* (0-1).

        Used once the camera has been handed to the privileged enrolment
        helper: the last frame stays on screen so the composition does not
        collapse to an empty circle, but it visibly steps back to say "this is
        no longer live".
        """
        dim = min(1.0, max(0.0, float(dim)))
        if abs(dim - self._dim) > 1e-3:
            self._dim = dim
            self.queue_draw()

    def set_natural_size(self, size: int) -> None:
        """Change the size the widget asks for when space allows.

        Used by the window's breakpoint to shrink the enrolment portrait on a
        narrow window rather than letting it force the window wider.
        """
        size = int(size)
        if size != self._natural:
            self._natural = size
            self.queue_resize()

    def set_show_ring(self, show: bool) -> None:
        """Draw (or hide) the hairline separating the circle from the page."""
        if show != self._show_ring:
            self._show_ring = bool(show)
            self.queue_draw()

    # -- geometry ----------------------------------------------------------

    def do_measure(self, orientation: Gtk.Orientation, for_size: int) -> tuple[int, int, int, int]:
        """Square, and willing to shrink.

        The minimum is deliberately small so the window can be resized down to
        a phone-sized column without the preview forcing horizontal overflow;
        the natural size is what the layout gets on a large display.
        """
        del orientation, for_size
        return (96, self._natural, -1, -1)

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        width = self.get_width()
        height = self.get_height()
        if width <= 0 or height <= 0:
            return

        side = float(min(width, height))
        bounds = Graphene.Rect().init(
            (width - side) / 2.0, (height - side) / 2.0, side, side
        )

        circle = Gsk.RoundedRect()
        circle.init_from_rect(bounds, side / 2.0)

        snapshot.push_rounded_clip(circle)
        try:
            if self._texture is not None:
                snapshot.append_texture(self._texture, self._cover_rect(bounds))
            else:
                # A flat wash of the foreground colour: legible in both themes
                # without inventing a colour that belongs to neither.
                snapshot.append_color(with_alpha(self.get_color(), 0.07), bounds)
            if self._dim > 0.0:
                snapshot.append_color(_rgb((0.0, 0.0, 0.0), 0.55 * self._dim), bounds)
        finally:
            snapshot.pop()

        if self._show_ring:
            edge = with_alpha(self.get_color(), 0.16 if self._texture else 0.10)
            snapshot.append_border(circle, [1.0, 1.0, 1.0, 1.0], [edge, edge, edge, edge])

    def _cover_rect(self, bounds: Graphene.Rect) -> Graphene.Rect:
        """Where to draw the texture so it fills *bounds* without distortion."""
        assert self._texture is not None
        tex_w = float(self._texture.get_width())
        tex_h = float(self._texture.get_height())
        if tex_w <= 0 or tex_h <= 0:  # pragma: no cover - malformed texture
            return bounds

        scale = max(bounds.size.width / tex_w, bounds.size.height / tex_h)
        draw_w = tex_w * scale
        draw_h = tex_h * scale
        return Graphene.Rect().init(
            bounds.origin.x + (bounds.size.width - draw_w) / 2.0,
            bounds.origin.y + (bounds.size.height - draw_h) / 2.0,
            draw_w,
            draw_h,
        )


# --------------------------------------------------------------------------
# one moment of the dial, resolved into primitives
# --------------------------------------------------------------------------
#
# The dial is specified once and *painted* twice: by Gsk inside :class:`FaceDial`
# (see the module docstring for why cairo cannot be used there), and by cairo in
# ``tools/render_dial.py``, which renders reference frames to PNG with no display
# attached at all.  Letting each painter re-derive 48 tick endpoints from the
# same ratios is precisely how the GNOME Shell copy of this dial and this one
# would silently drift apart, so neither painter is allowed to know any
# geometry: :meth:`DialModel.build_frame` resolves an instant of the animation
# into the primitives below, and a painter only has to know how to stroke a
# line, fill a radial gradient, and stroke a polyline.
#
# Colours are plain ``(r, g, b, a)`` floats rather than :class:`Gdk.RGBA` so the
# frame carries no toolkit types and can be produced, inspected and asserted on
# in a test with nothing initialised.
#
# Coordinates are absolute pixels with the ring's transform already applied: the
# success spring's scale and the failure shake's offset are baked into the
# numbers, and into :attr:`DialFrame.tick_width`, because scaling a canvas
# scales its stroke width too and a frame that forgot that would draw a ring
# with hairline ticks.  A painter therefore needs no transform stack.

class DialTick(NamedTuple):
    """One radial tick mark, as a stroked segment."""

    x0: float
    y0: float
    x1: float
    y1: float
    rgba: tuple[float, float, float, float]


class DialGlow(NamedTuple):
    """The soft light on the leading tick of a progress fill.

    A radial gradient from :attr:`rgba` at the centre to the same colour at
    zero alpha at :attr:`radius`.
    """

    cx: float
    cy: float
    radius: float
    rgba: tuple[float, float, float, float]


class DialCheck(NamedTuple):
    """The success checkmark, as far as it has been drawn.

    Two or three points: the polyline is truncated mid-stroke rather than
    dashed, so a painter draws it with one round-capped, round-joined stroke
    and gets the "pen moving" reading the spec asks for.
    """

    points: tuple[tuple[float, float], ...]
    width: float
    rgba: tuple[float, float, float, float]


class DialFrame(NamedTuple):
    """Everything on screen at one instant, in painting order.

    Glow first (it is spill *under* the ring, not a disc laid over it), then
    the ticks, then the checkmark.
    """

    width: float
    height: float
    tick_width: float
    ticks: tuple[DialTick, ...]
    glow: DialGlow | None
    check: DialCheck | None


# --------------------------------------------------------------------------
# DialModel
# --------------------------------------------------------------------------

class DialModel:
    """The dial's state machine and geometry, with no toolkit in it.

    Every number ``docs/ANIMATION.md`` specifies is computed here, and nothing
    here touches GTK: the caller supplies the clock (monotonic microseconds),
    the palette and the reduced-motion flag, and gets back a
    :class:`DialFrame`.  :class:`FaceDial` is a thin widget around one of
    these; ``tools/render_dial.py`` drives another with a synthetic clock to
    render the reference frames.  That split is what makes it possible to prove
    the drawing is right without a display, and it is why the offline renderer
    cannot quietly disagree with the shipping widget -- there is only one
    implementation to disagree with.

    Two implementation notes that are not obvious from the arithmetic:

    *The wave's phase is a function of absolute time,* ``((now - epoch) / lap)
    mod 1``, exactly as the spec writes it -- not a per-frame delta accumulated
    into a counter.  A delta drifts when frames are dropped; a function of the
    clock cannot.  Changing lap (idle to scanning) rewrites the epoch so the
    phase at that instant is unchanged, which is what stops the lit arc from
    teleporting when the speed changes.

    *The brightness change is what cross-fades, not the speed.*  The spec's
    200 ms state cross-fade is applied to the resting opacity and to the
    breathing amplitude; the lap switches at once.  Interpolating the lap would
    mean interpolating the phase's denominator, and the phase would stop being
    a function of absolute time for those 200 ms.  Nobody can see a speed
    change without an accompanying position jump, and there is no jump.
    """

    STATE_IDLE: Final[str] = "idle"
    STATE_SCANNING: Final[str] = "scanning"
    STATE_PROGRESS: Final[str] = "progress"
    STATE_SUCCESS: Final[str] = "success"
    STATE_FAILURE: Final[str] = "failure"

    #: The states that run indefinitely, as opposed to the two bounded beats.
    #: They are also exactly the states a beat may freeze *from*: freezing a
    #: frozen ring (failure straight into success) would capture a ring that
    #: was already mid-transition and compound the two.
    CONTINUOUS_STATES: Final[frozenset[str]] = frozenset(
        {STATE_IDLE, STATE_SCANNING, STATE_PROGRESS}
    )

    def __init__(self, now_us: int) -> None:
        self._state = self.STATE_IDLE
        self._state_start_us = now_us

        # Wave clock.  See the class docstring for why this is an epoch rather
        # than an accumulator.
        self._wave_epoch_us = now_us
        self._lap_ms = DIAL_IDLE_LAP_MS
        self._breathe_epoch_us = now_us

        # 0 = idle resting opacity, 1 = scanning resting opacity.  Stored as a
        # weight rather than as an alpha so a mid-animation theme switch
        # re-resolves against the new palette instead of keeping dark values.
        self._brightness = _Tween(0.0)
        #: Gate on the breathing amplitude, so scanning breathes in and out
        #: rather than starting and stopping mid-cycle.
        self._breathe = _Tween(0.0)
        self._fraction = _Tween(0.0)

        # The ring as it stood when a terminal state began.  The spec's success
        # and failure are both defined as transitions *from* the live
        # appearance ("the wave stops", "ticks desaturate"), so the beats need
        # to keep evaluating the previous state at a frozen phase.
        self._frozen_phase = 0.0
        self._frozen_state = self.STATE_IDLE
        self._frozen_fraction = 0.0
        self._frozen_brightness = 0.0

    # -- geometry the caller needs ----------------------------------------

    @staticmethod
    def inner_diameter(dial_size: int) -> int:
        """Diameter of the circle this dial encloses, at *dial_size* pixels.

        ``2 * 0.86 * 0.42 * size``, from the spec's "preview circle sits at
        0.86 x R".  Exposed so the enrolment wizard sizes its camera preview
        from the relationship rather than from a magic number that would
        silently stop matching if the ratios ever moved.
        """
        return int(round(2.0 * DIAL_INNER_RATIO * DIAL_RADIUS_RATIO * dial_size))

    @staticmethod
    def success_duration_ms(reduced: bool = False) -> float:
        """How long the success beats last, honestly.

        Callers sequence real work against this -- the wizard holds the capture
        screen until the beats are done -- so it has to tell the truth about
        the reduced-motion case, where success is a single 200 ms fade.
        """
        return DIAL_CROSSFADE_MS if reduced else DIAL_SUCCESS_MS

    @staticmethod
    def failure_duration_ms(reduced: bool = False) -> float:
        """How long failure runs before it hands back to idle."""
        return DIAL_CROSSFADE_MS if reduced else DIAL_SHAKE_MS

    # -- state -------------------------------------------------------------

    @property
    def state(self) -> str:
        return self._state

    @property
    def fraction(self) -> float:
        return self._fraction.target

    def set_progress(self, fraction: float, now_us: int, *, animate: bool) -> bool:
        """Move the fill to *fraction* and enter :attr:`STATE_PROGRESS`.

        Reduced motion snaps rather than tweens: the spec makes the non-idle
        states static, and the fill's *value* is the information -- the tween
        is presentation.
        """
        fraction = _clamp01(float(fraction))
        if animate:
            self._fraction.animate(fraction, now_us, DIAL_PROGRESS_MS, ease_out_cubic)
        else:
            self._fraction.set(fraction)
        return self.enter(self.STATE_PROGRESS, now_us)

    def reset(self, now_us: int) -> bool:
        self._fraction.set(0.0)
        return self.enter(self.STATE_IDLE, now_us)

    def enter(self, state: str, now_us: int) -> bool:
        """Move to *state*.  Returns whether anything actually restarted."""
        if state in (self.STATE_SUCCESS, self.STATE_FAILURE):
            # Freeze the ring as it stands.  Storing the *inputs* (phase,
            # fraction, brightness) rather than 48 resolved colours means the
            # frozen ring still re-resolves correctly if the theme changes
            # halfway through the beat.
            self._frozen_phase = self.phase_at(now_us)
            self._frozen_brightness = self._brightness.value(now_us)
            self._frozen_fraction = self._fraction.value(now_us)
            self._frozen_state = (
                self._state if self._state in self.CONTINUOUS_STATES else self.STATE_IDLE
            )

        # Re-entering idle or scanning must not restart anything: the wizard
        # calls set_scanning() on every preview frame it likes the look of, and
        # restarting the cross-fade fifteen times a second would make the ring
        # pulse at the camera's frame rate.  The other three states are always
        # restarted -- progress because the fill tween is measured from this
        # timestamp, success and failure because replaying the beat is the
        # whole point of asking for it again.
        restart = state != self._state or state not in (
            self.STATE_IDLE,
            self.STATE_SCANNING,
        )
        if not restart:
            return False

        self._state = state
        self._state_start_us = now_us

        crossfade = DIAL_CROSSFADE_MS
        if state == self.STATE_SCANNING:
            self._set_lap(DIAL_SCAN_LAP_MS, now_us)
            self._brightness.animate(1.0, now_us, crossfade, ease_out_quad)
            if self._breathe.target < 1.0:
                # Start the breath at its trough, so the ring swells into
                # scanning instead of being caught mid-inhale.
                self._breathe_epoch_us = now_us
            self._breathe.animate(1.0, now_us, crossfade, ease_out_quad)
        elif state in (self.STATE_IDLE, self.STATE_PROGRESS):
            # The spec's progress state leaves the ticks above the head
            # "idle", which is the idle rendering -- wave and all.  A frozen
            # ring above a moving fill would read as a half-broken widget.
            self._set_lap(DIAL_IDLE_LAP_MS, now_us)
            self._brightness.animate(0.0, now_us, crossfade, ease_out_quad)
            self._breathe.animate(0.0, now_us, crossfade, ease_out_quad)
        return True

    # -- the wave clock ----------------------------------------------------

    def elapsed_ms(self, now_us: int) -> float:
        """Milliseconds since the current state began."""
        return (now_us - self._state_start_us) / 1000.0

    def phase_at(self, now_us: int) -> float:
        """Position of the wave head, in turns clockwise from twelve o'clock."""
        return (((now_us - self._wave_epoch_us) / 1000.0) / self._lap_ms) % 1.0

    def _set_lap(self, lap_ms: float, now_us: int) -> None:
        """Change the wave's speed without moving the wave.

        Rewrites the epoch so ``phase_at(now_us)`` is unchanged.  Without this,
        idle -> scanning would divide the same elapsed time by a smaller lap
        and the lit arc would jump to a different point on the ring.
        """
        if abs(lap_ms - self._lap_ms) < 1e-6:
            return
        phase = self.phase_at(now_us)
        self._lap_ms = lap_ms
        self._wave_epoch_us = now_us - int(phase * lap_ms * 1000.0)

    # -- animation ---------------------------------------------------------

    def needs_frames(self, now_us: int, reduced: bool) -> bool:
        """Whether the dial still has something to draw next frame."""
        if self._state in (self.STATE_SUCCESS, self.STATE_FAILURE):
            # Both are bounded.  Asked after they have run out -- on remap, for
            # instance -- the answer is no: success holds its final frame and
            # failure has already handed back to idle.
            limit = (
                self.success_duration_ms(reduced)
                if self._state == self.STATE_SUCCESS
                else self.failure_duration_ms(reduced)
            )
            return self.elapsed_ms(now_us) < limit
        if not reduced:
            return self._state in self.CONTINUOUS_STATES
        # Static states still need frames while a cross-fade is settling: a
        # fade is not the motion that reduced-motion is about, and the spec
        # asks for one on success and failure.
        return self._brightness.active or self._breathe.active or self._fraction.active

    def advance(self, now_us: int, reduced: bool) -> bool:
        """Pump the clock one frame.  Returns whether more frames are wanted."""
        if self._state == self.STATE_SUCCESS:
            # Success holds its final frame; there is nothing after it to
            # animate, so the caller drops the callback and the last snapshot
            # stands.
            return self.elapsed_ms(now_us) < self.success_duration_ms(reduced)

        if self._state == self.STATE_FAILURE:
            if self.elapsed_ms(now_us) < self.failure_duration_ms(reduced):
                return True
            # "Then fall back to idle so the user can simply try again."
            self.enter(self.STATE_IDLE, now_us)
            return self.needs_frames(now_us, reduced)

        # Pump the tweens so they retire on time even in a frame where nothing
        # reads them, then keep the clock only if something still moves.
        self._brightness.value(now_us)
        self._breathe.value(now_us)
        self._fraction.value(now_us)
        return self.needs_frames(now_us, reduced)

    # -- appearance --------------------------------------------------------

    def _wave_appearance(
        self,
        index: int,
        phase: float,
        base_alpha: float,
        palette: _DialPalette,
        reduced: bool,
    ) -> tuple[tuple[float, float, float], float, float]:
        """``(rgb, alpha, length)`` for one tick of the travelling wave.

        *length* is a multiple of L.  Reduced motion collapses this to the
        even ring the spec describes: same colour, same resting opacity, no
        boost -- so idle is still visibly dimmer than scanning and the states
        remain distinguishable without a single moving pixel.
        """
        if reduced:
            return (palette.tick, base_alpha, 1.0)

        # d(i): wrapped distance from tick i to the wave head, in turns.
        distance = abs(index / DIAL_TICKS - phase)
        distance = min(distance, 1.0 - distance)
        boost = math.exp(-((distance / DIAL_WAVE_SIGMA) ** 2))
        return (
            palette.tick,
            base_alpha + palette.boost_alpha * boost,
            1.0 + DIAL_WAVE_LENGTH_BOOST * boost,
        )

    def _progress_appearance(
        self,
        index: int,
        phase: float,
        fraction: float,
        base_alpha: float,
        palette: _DialPalette,
        reduced: bool,
    ) -> tuple[tuple[float, float, float], float, float]:
        rgb, alpha, length = self._wave_appearance(
            index, phase, base_alpha, palette, reduced
        )

        # A hard threshold would make the fill advance in 48 audible clicks
        # however smoothly the fraction tweens.  Blending across exactly one
        # tick's worth of turn gives the head a soft leading edge and keeps the
        # motion continuous, without ever lighting a tick the fill has not
        # reached.
        fill = _clamp01((fraction - index / DIAL_TICKS) * DIAL_TICKS)
        if fill <= 0.0:
            return (rgb, alpha, length)

        return (
            _lerp_rgb(rgb, palette.accent, fill),
            _lerp(alpha, 1.0, fill),
            _lerp(length, DIAL_FILL_LENGTH, fill),
        )

    def _frozen_appearance(
        self, index: int, palette: _DialPalette, reduced: bool
    ) -> tuple[tuple[float, float, float], float, float]:
        """The tick as it looked the instant a terminal state began."""
        base_alpha = _lerp(
            palette.idle_alpha, palette.scan_alpha, self._frozen_brightness
        )
        if self._frozen_state == self.STATE_PROGRESS:
            return self._progress_appearance(
                index, self._frozen_phase, self._frozen_fraction,
                base_alpha, palette, reduced,
            )
        return self._wave_appearance(
            index, self._frozen_phase, base_alpha, palette, reduced
        )

    def tick_appearance(
        self,
        index: int,
        now_us: int,
        palette: _DialPalette,
        reduced: bool,
    ) -> tuple[tuple[float, float, float], float, float]:
        """``(rgb, alpha, length multiple of L)`` for tick *index* right now."""
        elapsed_ms = self.elapsed_ms(now_us)

        if self._state == self.STATE_SUCCESS:
            source = self._frozen_appearance(index, palette, reduced)
            if reduced:
                # No converge beat: the ring is simply already converged, and
                # only the checkmark fades in.
                converge = fade = 1.0
            else:
                converge = ease_out_cubic(elapsed_ms / DIAL_CONVERGE_MS)
                fade = ease_out_cubic(
                    (elapsed_ms - DIAL_CHECK_START_MS) / DIAL_CHECK_MS
                )
            rgb = _lerp_rgb(source[0], palette.success, converge)
            alpha = _lerp(source[1], 1.0, converge)
            # Beat three dims the converged ring so the mark reads over it.
            alpha = _lerp(alpha, DIAL_SUCCESS_TICK_ALPHA, fade)
            length = _lerp(source[2], DIAL_FILL_LENGTH, converge)
            return (rgb, alpha, length)

        if self._state == self.STATE_FAILURE:
            source = self._frozen_appearance(index, palette, reduced)
            # "0 -> 300ms  ticks desaturate to the muted colour and return to
            # base length."  Under reduced motion there is no shake to carry
            # the message, so the dim is the whole of it -- one 200 ms fade.
            settle = (
                ease_out_quad(elapsed_ms / DIAL_CROSSFADE_MS)
                if reduced
                else ease_out_cubic(elapsed_ms / DIAL_SETTLE_MS)
            )
            return (
                _lerp_rgb(source[0], palette.tick, settle),
                _lerp(source[1], palette.muted_alpha, settle),
                _lerp(source[2], 1.0, settle),
            )

        phase = self.phase_at(now_us)
        base_alpha = _lerp(
            palette.idle_alpha, palette.scan_alpha, self._brightness.value(now_us)
        )
        if self._state == self.STATE_PROGRESS:
            return self._progress_appearance(
                index, phase, self._fraction.value(now_us),
                base_alpha, palette, reduced,
            )
        return self._wave_appearance(index, phase, base_alpha, palette, reduced)

    def ring_transform(
        self, now_us: int, radius: float, reduced: bool
    ) -> tuple[float, float]:
        """``(scale, x offset)`` for the whole ring at this instant."""
        if reduced:
            # "no wave, no shake, no spring."
            return (1.0, 0.0)

        elapsed_ms = self.elapsed_ms(now_us)

        if self._state == self.STATE_SUCCESS:
            # "120 -> 420ms  ring scale 1.0 -> 1.06 -> 1.0, overshoot spring
            # (ease-out-back, overshoot 1.7)."  The spring is on the way out --
            # the pop overshoots 1.06 and settles back onto it -- and the
            # return is a plain deceleration, because a second spring on the
            # way home turns one confident beat into a wobble.  The rise takes
            # DIAL_SPRING_RISE of the beat; the Shell extension uses the same
            # split, and the two must agree or the pop peaks at a different
            # moment in each runtime.
            u = _clamp01((elapsed_ms - DIAL_SPRING_START_MS) / DIAL_SPRING_MS)
            if u <= 0.0 or u >= 1.0:
                return (1.0, 0.0)
            if u < DIAL_SPRING_RISE:
                swell = ease_out_back(u / DIAL_SPRING_RISE)
            else:
                swell = 1.0 - ease_out_cubic(
                    (u - DIAL_SPRING_RISE) / (1.0 - DIAL_SPRING_RISE)
                )
            return (1.0 + DIAL_SPRING_SCALE * swell, 0.0)

        if self._state == self.STATE_FAILURE:
            # "x = A * sin(2*pi*3*u) * (1-u), A = 0.035 * R, u = t/420ms."
            u = elapsed_ms / DIAL_SHAKE_MS
            return (1.0, DIAL_SHAKE_AMPLITUDE * radius * damped_sine(u))

        # Breathing, gated so it fades in with the scanning cross-fade rather
        # than switching on at whatever point of the sine the state changed.
        gate = self._breathe.value(now_us)
        if gate <= 0.001:
            return (1.0, 0.0)
        cycle = (((now_us - self._breathe_epoch_us) / 1000.0) % DIAL_BREATHE_MS)
        swell = 0.5 - 0.5 * math.cos(2.0 * math.pi * cycle / DIAL_BREATHE_MS)
        return (1.0 + DIAL_BREATHE_AMPLITUDE * gate * swell, 0.0)

    def check_draw(self, now_us: int, reduced: bool) -> tuple[float, float] | None:
        """``(stroke fraction drawn, opacity)``, or ``None`` if there is no mark."""
        if self._state != self.STATE_SUCCESS:
            return None

        elapsed_ms = self.elapsed_ms(now_us)
        if reduced:
            # "success draws the checkmark with a single 200 ms fade."
            drawn, alpha = 1.0, ease_out_quad(elapsed_ms / DIAL_CROSSFADE_MS)
        else:
            drawn = ease_out_cubic(
                (elapsed_ms - DIAL_CHECK_START_MS) / DIAL_CHECK_MS
            )
            alpha = 1.0
        if drawn <= 0.0 or alpha <= 0.0:
            return None
        return (drawn, alpha)

    # -- the frame ---------------------------------------------------------

    def build_frame(
        self,
        width: float,
        height: float,
        now_us: int,
        palette: _DialPalette,
        reduced: bool,
    ) -> DialFrame:
        """Resolve this instant into painter-agnostic primitives."""
        side = float(min(width, height))
        if side <= 0.0:
            return DialFrame(width, height, 0.0, (), None, None)

        radius = DIAL_RADIUS_RATIO * side
        base_length = DIAL_TICK_BASE_RATIO * radius
        tick_width = max(DIAL_TICK_WIDTH_MIN, DIAL_TICK_WIDTH_RATIO * radius)
        cx, cy = width / 2.0, height / 2.0

        scale, offset_x = self.ring_transform(now_us, radius, reduced)

        def place(x: float, y: float) -> tuple[float, float]:
            """Apply the ring's pose: scale about the centre, then shake."""
            return (cx + (x - cx) * scale + offset_x, cy + (y - cy) * scale)

        ticks: list[DialTick] = []
        for index in range(DIAL_TICKS):
            rgb, alpha, length = self.tick_appearance(index, now_us, palette, reduced)
            if alpha <= 0.004:
                continue  # below one step of 8-bit alpha: a primitive for nothing

            # The spec's peak tick length is an envelope for the whole widget,
            # not just for the wave, so it is enforced here rather than in each
            # state's arithmetic.
            length = min(length, _DIAL_TICK_MAX_MULTIPLE) * base_length

            # Index 0 is twelve o'clock and the ring runs clockwise; y grows
            # downwards, hence the negated cosine.
            angle = 2.0 * math.pi * index / DIAL_TICKS
            dx, dy = math.sin(angle), -math.cos(angle)

            # Ticks are centred on R rather than grown outwards from it, so the
            # ring's radius stays put as they lengthen and the success spring
            # cannot push the longest ones outside the widget.
            inner = radius - length / 2.0
            outer = radius + length / 2.0

            x0, y0 = place(cx + dx * inner, cy + dy * inner)
            x1, y1 = place(cx + dx * outer, cy + dy * outer)
            ticks.append(DialTick(x0, y0, x1, y1, (*rgb, min(1.0, alpha))))

        glow: DialGlow | None = None
        if self._state == self.STATE_PROGRESS:
            # The soft light the spec puts on the leading tick of the fill.
            # Skipped at zero, where there is no head yet -- a glow at 12
            # o'clock before anything has been captured would claim progress
            # that has not happened.
            fraction = self._fraction.value(now_us)
            if fraction > 0.0:
                angle = 2.0 * math.pi * fraction
                hx, hy = place(
                    cx + radius * math.sin(angle), cy - radius * math.cos(angle)
                )
                glow = DialGlow(
                    hx, hy,
                    base_length * DIAL_HEAD_GLOW_MULTIPLE * scale,
                    (*palette.accent, DIAL_HEAD_GLOW_ALPHA),
                )

        check: DialCheck | None = None
        drawing = self.check_draw(now_us, reduced)
        if drawing is not None:
            drawn, alpha = drawing
            # "normalised to the ring's inner box (unit square, origin
            # top-left)": the box the ring encloses, i.e. the same circle the
            # preview sits on.  Deliberately *not* run through place(): beats
            # two and three overlap between 260 ms and 420 ms, and a mark that
            # rode the spring would appear to wobble as it was being drawn.
            # The ring springs; the thing it reveals holds still.
            box = 2.0 * DIAL_INNER_RATIO * radius
            ox, oy = cx - box / 2.0, cy - box / 2.0
            p0, p1, p2 = (
                (ox + px * box, oy + py * box) for px, py in DIAL_CHECK_POINTS
            )

            # The split is by draw progress, not by arc length: the spec gives
            # the short leg 38% of the time deliberately, so it does not look
            # rushed.
            if drawn <= DIAL_CHECK_FIRST_LEG:
                points = (p0, _lerp_point(p0, p1, drawn / DIAL_CHECK_FIRST_LEG))
            else:
                tail = (drawn - DIAL_CHECK_FIRST_LEG) / (1.0 - DIAL_CHECK_FIRST_LEG)
                points = (p0, p1, _lerp_point(p1, p2, tail))

            check = DialCheck(
                points, DIAL_CHECK_WIDTH * box, (*palette.success, alpha)
            )

        return DialFrame(width, height, tick_width * scale, tuple(ticks), glow, check)


def paint_dial_frame_gsk(snapshot: Gtk.Snapshot, frame: DialFrame) -> None:
    """Paint a :class:`DialFrame` with GSK render nodes.

    The Gsk half of the "specified once, painted twice" split; the cairo half
    lives in ``tools/render_dial.py``.  Neither knows any geometry, so neither
    can drift from the spec on its own.
    """
    if frame.glow is not None and frame.glow.radius > 0.0:
        glow = frame.glow
        accent = _rgb(glow.rgba[:3])
        snapshot.append_radial_gradient(
            Graphene.Rect().init(
                glow.cx - glow.radius, glow.cy - glow.radius,
                2.0 * glow.radius, 2.0 * glow.radius,
            ),
            _point(glow.cx, glow.cy),
            glow.radius,
            glow.radius,
            0.0,
            1.0,
            [
                _stop(0.0, with_alpha(accent, glow.rgba[3])),
                _stop(1.0, with_alpha(accent, 0.0)),
            ],
        )

    if frame.ticks and frame.tick_width > 0.0:
        # One Gsk.Stroke for all 48: it is immutable state, and rebuilding it
        # per tick would allocate 48 objects a frame for no difference.
        stroke = _stroke(frame.tick_width)
        for tick in frame.ticks:
            builder = Gsk.PathBuilder.new()
            builder.move_to(tick.x0, tick.y0)
            builder.line_to(tick.x1, tick.y1)
            snapshot.append_stroke(
                builder.to_path(), stroke, _rgb(tick.rgba[:3], tick.rgba[3])
            )

    if frame.check is not None:
        check = frame.check
        snapshot.append_stroke(
            _polyline(check.points),
            _stroke(check.width),
            _rgb(check.rgba[:3], check.rgba[3]),
        )


# --------------------------------------------------------------------------
# FaceDial
# --------------------------------------------------------------------------

class FaceDial(_Animated, _Canvas):
    """The 48-tick face dial specified by ``docs/ANIMATION.md``.

    One object with five states -- :attr:`STATE_IDLE`, :attr:`STATE_SCANNING`,
    :attr:`STATE_PROGRESS`, :attr:`STATE_SUCCESS`, :attr:`STATE_FAILURE` --
    rather than five widgets swapped in a stack, because the states are
    *continuous* with one another: scanning is idle brightened and sped up,
    success starts from the ring exactly as it stood, failure returns to idle.
    A stack would cross-fade between two renderings of the same ring, which is
    the one thing that would make the seams visible.

    The widget is deliberately thin.  All of the motion and all of the geometry
    is in :class:`DialModel`, which knows nothing about GTK; this class owns
    only the things a widget has to own -- the frame clock, the accessibility
    properties, and the translation of a :class:`DialFrame` into render nodes.
    That is what lets ``tools/render_dial.py`` prove the drawing is correct
    headlessly against the very same code that ships.

    Exposed to assistive technology as a progress bar with a real value and a
    per-state description, so the information is never carried by motion alone.
    """

    __gtype_name__ = "IrisFaceDial"

    STATE_IDLE: Final[str] = DialModel.STATE_IDLE
    STATE_SCANNING: Final[str] = DialModel.STATE_SCANNING
    STATE_PROGRESS: Final[str] = DialModel.STATE_PROGRESS
    STATE_SUCCESS: Final[str] = DialModel.STATE_SUCCESS
    STATE_FAILURE: Final[str] = DialModel.STATE_FAILURE

    def __init__(
        self,
        natural_size: int = 300,
        label: str = "Enrolment progress",
        **kwargs: object,
    ) -> None:
        super().__init__(content_width=natural_size, content_height=natural_size, **kwargs)

        self._model = DialModel(self._now_us())

        self._success_text = "Face captured"
        self._failure_text = "That attempt did not work"

        self.set_can_target(False)  # decoration: never steal a click

        self.set_accessible_role(Gtk.AccessibleRole.PROGRESS_BAR)
        self.update_property([Gtk.AccessibleProperty.LABEL], [label])
        self.update_property(
            [Gtk.AccessibleProperty.VALUE_MIN, Gtk.AccessibleProperty.VALUE_MAX],
            [0.0, 1.0],
        )
        self._announce()

        # A dial nobody can see must not hold the frame clock open, one that
        # comes back must pick the wave up again, and one whose user turns
        # animation back on must start moving; _Animated does all three.
        self._connect_visibility()

    # -- geometry the caller needs ----------------------------------------

    @staticmethod
    def inner_diameter(dial_size: int) -> int:
        """Diameter of the circle this dial encloses, at *dial_size* pixels."""
        return DialModel.inner_diameter(dial_size)

    @staticmethod
    def success_duration_ms() -> float:
        """How long :meth:`set_success` will be busy, honestly.

        Callers sequence real work against this -- the wizard holds the capture
        screen until the beats are done -- so it has to tell the truth about
        the reduced-motion case, where success is a single 200 ms fade.
        """
        return DialModel.success_duration_ms(not animations_enabled())

    @staticmethod
    def failure_duration_ms() -> float:
        """How long :meth:`set_failure` will be busy before it returns to idle."""
        return DialModel.failure_duration_ms(not animations_enabled())

    # -- state -------------------------------------------------------------

    @property
    def state(self) -> str:
        return self._model.state

    @property
    def fraction(self) -> float:
        return self._model.fraction

    def set_idle(self) -> None:
        """Waiting, no face yet."""
        self._model.enter(self.STATE_IDLE, self._now_us())
        self._settle()

    def set_scanning(self) -> None:
        """A face is present and being worked on."""
        self._model.enter(self.STATE_SCANNING, self._now_us())
        self._settle()

    def set_progress(self, fraction: float, *, animate: bool = True) -> None:
        """Enrolment progress, 0-1.  Enters :attr:`STATE_PROGRESS`."""
        self._model.set_progress(
            fraction,
            self._now_us(),
            animate=animate and animations_enabled(),
        )
        self._settle()

    #: Retained under its previous name: this is what the enrolment wizard has
    #: always called, and the semantics (move the fill, tweened) are unchanged.
    set_fraction = set_progress

    def set_success(self, message: str = "Face captured") -> None:
        """Play the three-beat success sequence and hold on its last frame."""
        self._success_text = message
        self._model.enter(self.STATE_SUCCESS, self._now_us())
        self._settle()

    def set_failure(self, message: str = "That attempt did not work") -> None:
        """Shake, desaturate, and fall back to idle so the user can retry."""
        self._failure_text = message
        self._model.enter(self.STATE_FAILURE, self._now_us())
        self._settle()

    def reset(self) -> None:
        """Back to a cleared, idle dial."""
        self._model.reset(self._now_us())
        self._settle()

    def _settle(self) -> None:
        """Announce, ask for frames and repaint after any state change.

        Always announced and always redrawn, even when the state was already
        current: set_progress() reaches here with a new fraction to say.
        """
        self._announce()
        self._start_ticking()
        self.queue_draw()

    # -- accessibility -----------------------------------------------------

    def _announce(self) -> None:
        """Say the state in words.

        ANIMATION.md: "every state must also be announced as text for screen
        readers".  The progress-bar value carries the number and the
        description carries the state, so a reader that only speaks values
        still gets the percentage and one that speaks descriptions gets the
        sentence.
        """
        fraction = self._model.fraction
        percent = round(fraction * 100)
        text = {
            self.STATE_IDLE: "Waiting for a face",
            self.STATE_SCANNING: "Looking for your face",
            self.STATE_PROGRESS: f"Capturing your face, {percent} per cent",
            self.STATE_SUCCESS: self._success_text,
            self.STATE_FAILURE: self._failure_text,
        }[self._model.state]
        self.update_property(
            [
                Gtk.AccessibleProperty.VALUE_NOW,
                Gtk.AccessibleProperty.VALUE_TEXT,
                Gtk.AccessibleProperty.DESCRIPTION,
            ],
            [fraction, text, text],
        )

    # -- animation ---------------------------------------------------------

    def _advance(self, now_us: int) -> bool:
        was = self._model.state
        keep = self._model.advance(now_us, not animations_enabled())
        if self._model.state != was:
            # Failure hands itself back to idle; the description has to follow
            # it or a screen reader is left saying the attempt failed while the
            # ring is quietly waiting for another one.
            self._announce()
        return keep

    # -- drawing -----------------------------------------------------------

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        width, height = self.get_width(), self.get_height()
        if min(width, height) <= 0:
            return

        # The palette is resolved per snapshot rather than cached: libadwaita
        # can switch the colour scheme mid-animation (night mode, a portal),
        # and a dial that finished its success beat in yesterday's palette
        # would be the only stale thing on screen.
        paint_dial_frame_gsk(
            snapshot,
            self._model.build_frame(
                float(width),
                float(height),
                self._now_us(),
                dial_palette(),
                not animations_enabled(),
            ),
        )


#: The dial replaced a plain arc-based progress ring.  The old name is kept as
#: an alias because it is what the wizard called for the whole of its previous
#: life and an out-of-tree caller may still; the constructor's ``thickness``
#: argument is gone, since the spec derives the tick width from R.
ProgressRing = FaceDial


# --------------------------------------------------------------------------
# ScanOverlay
# --------------------------------------------------------------------------

class ScanOverlay(_Animated, _Canvas):
    """A soft band of light sweeping across the preview while capture runs.

    Purely an activity signal, and shaped like one: it carries no information,
    so it is marked ``PRESENTATION`` for assistive technology -- the real state
    is announced by the progress ring and the hint label -- and it never
    accepts input.  It fades in and out rather than appearing, because an
    element that pops into existence over someone's face is startling.

    Clipped to the same inscribed circle as :class:`CameraPreview`, which is
    what makes the two read as one object when stacked in a
    :class:`Gtk.Overlay`.
    """

    __gtype_name__ = "IrisScanOverlay"

    def __init__(self, natural_size: int = 260, **kwargs: object) -> None:
        super().__init__(content_width=natural_size, content_height=natural_size, **kwargs)
        self._running = False
        self._intensity = _Tween(0.0)
        self._phase_start_us = 0

        self.set_can_target(False)
        self.set_accessible_role(Gtk.AccessibleRole.PRESENTATION)
        self._connect_visibility()

    # -- control -----------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> None:
        """Begin sweeping, fading in from wherever the overlay currently is."""
        if self._running:
            return
        self._running = True
        now = self._now_us()
        self._phase_start_us = now
        self._intensity.animate(1.0, now, SCAN_FADE_MS)
        self._start_ticking()

    def stop(self) -> None:
        """Fade out, then stop consuming frames."""
        if not self._running:
            return
        self._running = False
        self._intensity.animate(0.0, self._now_us(), SCAN_FADE_MS)
        self._start_ticking()

    # -- animation ---------------------------------------------------------

    def _advance(self, now_us: int) -> bool:
        # Keep the frame clock only while there is something to see: the sweep
        # is running, or it is still fading out.
        intensity = self._intensity.value(now_us)
        if not animations_enabled():
            # Nothing sweeps, so frames are needed only while the fade runs.
            return self._intensity.active
        return self._running or self._intensity.active or intensity > 0.001

    # -- drawing -----------------------------------------------------------

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        width, height = self.get_width(), self.get_height()
        if width <= 0 or height <= 0:
            return

        now = self._now_us()
        intensity = self._intensity.value(now)
        if intensity <= 0.001:
            return

        side = float(min(width, height))
        radius = side / 2.0
        cx, cy = width / 2.0, height / 2.0
        bounds = Graphene.Rect().init(cx - radius, cy - radius, side, side)

        circle = Gsk.RoundedRect()
        circle.init_from_rect(bounds, radius)

        if animations_enabled():
            # Position within the sweep, from the frame clock so the speed is
            # the same at any refresh rate. Eased so the band slows at the
            # extremes instead of ricocheting off them.
            elapsed_ms = (now - self._phase_start_us) / 1000.0
            phase = (elapsed_ms % SCAN_CYCLE_MS) / SCAN_CYCLE_MS
            centre = (cy - radius) + ease_in_out_sine(phase) * (2.0 * radius)
        else:
            # Reduced motion: the band parks across the middle and only fades.
            # Removing it entirely would be defensible -- it is marked
            # PRESENTATION and carries no information -- but the circle visibly
            # changing when capture starts is worth keeping, and a stationary
            # wash is not motion.
            centre = cy

        accent = accent_rgba()
        band = radius * 0.55

        snapshot.push_rounded_clip(circle)
        try:
            # The band. Transparent at both ends, so the gradient's edge
            # colours extend to the rest of the circle as pure transparency.
            snapshot.append_linear_gradient(
                bounds,
                _point(cx, centre - band),
                _point(cx, centre + band),
                [
                    _stop(0.0, with_alpha(accent, 0.0)),
                    _stop(0.5, with_alpha(accent, 0.26 * intensity)),
                    _stop(1.0, with_alpha(accent, 0.0)),
                ],
            )
            # A brighter hairline at the centre of the band gives the sweep an
            # edge to follow; the gradient alone reads as a flicker.
            snapshot.append_linear_gradient(
                Graphene.Rect().init(cx - radius, centre - 1.0, 2.0 * radius, 2.0),
                _point(cx - radius, centre),
                _point(cx + radius, centre),
                [
                    _stop(0.0, with_alpha(accent, 0.0)),
                    _stop(0.5, with_alpha(accent, 0.55 * intensity)),
                    _stop(1.0, with_alpha(accent, 0.0)),
                ],
            )
        finally:
            snapshot.pop()


# --------------------------------------------------------------------------
# StatusBadge
# --------------------------------------------------------------------------

class StatusBadge(_Animated, _Canvas):
    """A checkmark or cross that draws itself once, then holds.

    The stroke-on animation is the point: a result that appears fully formed is
    easy to miss at the end of a task the user has been watching for several
    seconds, whereas one that is *drawn* pulls the eye to it.  It runs for
    680 ms end to end and never repeats -- a looping success mark would be
    decoration, and decoration on a security confirmation is noise.
    """

    __gtype_name__ = "IrisStatusBadge"

    STATE_IDLE: Final[str] = "idle"
    STATE_SUCCESS: Final[str] = "success"
    STATE_FAILURE: Final[str] = "failure"

    def __init__(self, natural_size: int = 88, thickness: float = 4.0, **kwargs: object) -> None:
        super().__init__(content_width=natural_size, content_height=natural_size, **kwargs)
        self._state = self.STATE_IDLE
        self._start_us = 0
        self._thickness = float(thickness)

        self.set_can_target(False)
        self.set_accessible_role(Gtk.AccessibleRole.IMG)
        self.update_property([Gtk.AccessibleProperty.LABEL], ["No result yet"])
        self._connect_visibility()

    # -- control -----------------------------------------------------------

    @property
    def state(self) -> str:
        return self._state

    def show_success(self, label: str = "Succeeded") -> None:
        self._show(self.STATE_SUCCESS, label)

    def show_failure(self, label: str = "Did not finish") -> None:
        self._show(self.STATE_FAILURE, label)

    def reset(self) -> None:
        self._state = self.STATE_IDLE
        self._stop_ticking()
        self.update_property([Gtk.AccessibleProperty.LABEL], ["No result yet"])
        self.queue_draw()

    def _show(self, state: str, label: str) -> None:
        self._state = state
        self._start_us = self._now_us()
        self.update_property([Gtk.AccessibleProperty.LABEL], [label])
        self._start_ticking()
        self.queue_draw()

    # -- animation ---------------------------------------------------------

    def _advance(self, now_us: int) -> bool:
        elapsed_ms = (now_us - self._start_us) / 1000.0
        if not animations_enabled():
            return elapsed_ms < DIAL_CROSSFADE_MS
        return elapsed_ms < (BADGE_CIRCLE_MS + BADGE_GLYPH_MS)

    def _phases(self) -> tuple[float, float]:
        """``(circle, glyph)`` completion, each 0-1, at the current time."""
        if self._state == self.STATE_IDLE:
            return (0.0, 0.0)
        if not animations_enabled():
            # The mark is already complete; only :meth:`_opacity` moves it,
            # matching the dial's reduced-motion success -- one 200 ms fade.
            return (1.0, 1.0)
        elapsed_ms = (self._now_us() - self._start_us) / 1000.0
        circle = ease_out_cubic(min(1.0, elapsed_ms / BADGE_CIRCLE_MS))
        glyph = ease_out_cubic(
            min(1.0, max(0.0, (elapsed_ms - BADGE_CIRCLE_MS) / BADGE_GLYPH_MS))
        )
        return (circle, glyph)

    def _opacity(self) -> float:
        """1, except during the reduced-motion fade-in."""
        if animations_enabled() or self._state == self.STATE_IDLE:
            return 1.0
        return ease_out_quad((self._now_us() - self._start_us) / 1000.0 / DIAL_CROSSFADE_MS)

    # -- drawing -----------------------------------------------------------

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        if self._state == self.STATE_IDLE:
            return

        width, height = self.get_width(), self.get_height()
        side = float(min(width, height))
        radius = side / 2.0 - self._thickness
        if radius <= 0:
            return

        circle_t, glyph_t = self._phases()
        colour = success_rgba() if self._state == self.STATE_SUCCESS else failure_rgba()
        opacity = self._opacity()
        if opacity <= 0.0:
            return
        colour = with_alpha(colour, opacity)
        cx, cy = width / 2.0, height / 2.0
        stroke = _stroke(self._thickness)

        # A soft disc behind the mark, so the glyph never sits on bare
        # background and keeps its contrast over a card or a photo.
        disc = Gsk.PathBuilder.new()
        disc.add_circle(_point(cx, cy), radius)
        snapshot.append_fill(
            disc.to_path(), Gsk.FillRule.WINDING, with_alpha(colour, 0.12 * opacity)
        )

        arc = _arc_path(cx, cy, radius, circle_t)
        if arc is not None:
            snapshot.append_stroke(arc, stroke, colour)

        if glyph_t <= 0.0:
            return

        for path in self._glyph_paths(cx, cy, radius, glyph_t):
            snapshot.append_stroke(path, stroke, colour)

    def _glyph_paths(
        self, cx: float, cy: float, radius: float, t: float
    ) -> list[Gsk.Path]:
        if self._state == self.STATE_SUCCESS:
            return [self._check_path(cx, cy, radius, t)]
        return self._cross_paths(cx, cy, radius, t)

    def _check_path(self, cx: float, cy: float, radius: float, t: float) -> Gsk.Path:
        """A two-segment tick, drawn end to end as one continuous stroke."""
        p0 = (cx - radius * 0.42, cy + radius * 0.02)
        p1 = (cx - radius * 0.12, cy + radius * 0.34)
        p2 = (cx + radius * 0.46, cy - radius * 0.32)

        # Split the progress by segment length so the pen speed stays constant
        # across the corner; a 50/50 split would visibly stall there.
        split = math.dist(p0, p1) / (math.dist(p0, p1) + math.dist(p1, p2))

        if t <= split:
            return _polyline([p0, _lerp_point(p0, p1, t / split)])
        return _polyline([p0, p1, _lerp_point(p1, p2, (t - split) / (1.0 - split))])

    def _cross_paths(
        self, cx: float, cy: float, radius: float, t: float
    ) -> list[Gsk.Path]:
        """Two strokes, drawn one after the other rather than together."""
        reach = radius * 0.38
        first = min(1.0, t / 0.5)
        second = max(0.0, (t - 0.5) / 0.5)

        a0, a1 = (cx - reach, cy - reach), (cx + reach, cy + reach)
        paths = [_polyline([a0, _lerp_point(a0, a1, first)])]

        if second > 0.0:
            b0, b1 = (cx + reach, cy - reach), (cx - reach, cy + reach)
            paths.append(_polyline([b0, _lerp_point(b0, b1, second)]))
        return paths
