/* Iris — GNOME Shell extension (Shell 48-50, ESM).
 *
 * WHAT THIS EXTENSION DOES, AND WHAT IT DELIBERATELY DOES NOT DO
 * --------------------------------------------------------------
 * It does NOT authenticate you. It cannot: GNOME Shell runs unprivileged in your
 * session, while the Iris daemon's socket is root-only (0600) by design, and the
 * decision to unlock belongs to PAM.
 *
 * The real mechanism is simpler than it looks. GNOME's unlock dialog and the GDM
 * greeter both open a PAM conversation the moment they appear. If pam_iris.so is
 * wired into the gdm-password stack, it runs at that instant, looks at the camera,
 * and either succeeds (you are let in without typing) or is ignored (you get the
 * password field you already had). No extension involvement is required for that
 * to work, which is exactly why it is trustworthy.
 *
 * So this extension is presentation and control:
 *   - on the lock screen / greeter, a calm "looking for your face" indicator so the
 *     pause before the password field is explained rather than mysterious;
 *   - in the session, a top-bar menu showing whether the daemon is healthy and
 *     whether you are enrolled, plus a way into the settings app.
 *
 * Anything it reports about daemon health comes from shelling out to the
 * unprivileged `iris status --json`. It never fabricates a confidence score, and
 * it never claims an authentication result it did not observe.
 *
 * THE DIAL
 * --------
 * The lock-screen indicator is a real drawn object — the 48-tick dial specified in
 * docs/ANIMATION.md — rendered with Cairo into an St.DrawingArea. The GTK4
 * enrolment app draws the same dial from the same numbers, so the two runtimes
 * look like one product.
 *
 * Three rules govern the motion, and they are all about not being a bad citizen
 * on a lock screen:
 *
 *   1. Every position is a function of elapsed monotonic time, never of a frame
 *      counter, so the dial runs at the same speed on a 60 Hz panel and on the
 *      144 Hz one this was written on, and a dropped frame costs nothing.
 *   2. The repaint timer exists only while something is actually moving. A
 *      permanently armed 60 Hz timeout behind a locked screen is a battery bug,
 *      and it is the kind that never gets attributed to the extension that
 *      caused it.
 *   3. Every geometry constant is a ratio of the widget's real allocation, so the
 *      dial is identical at 1x and on a 2560x1600 HiDPI panel.
 *
 * WHERE THE DIAL GOES
 * -------------------
 * Into the shell's own AuthPrompt, as a child at index 1 — under the avatar and
 * above the password field. Not into a group of our own, and not at a guessed
 * fraction of the monitor height.
 *
 * That distinction is the whole reason this file was rewritten. An earlier
 * version positioned the hint absolutely inside Main.layoutManager.modalDialogGroup
 * at ~68% of the screen. It reported itself as active and drew nothing, because
 * the lock screen's dialog does not live in that group at all: it lives in
 * ScreenShield._lockDialogGroup, which is translated a whole screen height off
 * the top while the curtain is up. The hint was being drawn perfectly, somewhere
 * nobody could see.
 *
 * Inside the AuthPrompt there is no positioning to get wrong. It is a stock
 * vertical St.BoxLayout, so a child is measured, spaced and allocated by the
 * shell; and both dialogs derive the auth column's size from the prompt's
 * get_preferred_size(), so a prompt that grew to fit the dial re-centres itself.
 * The same three lines work on the lock screen and at the greeter, because
 * LoginDialog *is* the unlockDialog constructor in gdm mode.
 *
 * WHAT DRIVES IT
 * --------------
 * AuthPrompt's `verification-status` property, which is its real state machine —
 * VERIFYING when PAM's conversation opens, VERIFICATION_SUCCEEDED when it is
 * granted, and so on. The extension observes those transitions and nothing else.
 * In particular the checkmark is never shown for a success it cannot attribute
 * to a silent PAM module; see IrisPromptBinding._sync().
 */

import GObject from 'gi://GObject';
import St from 'gi://St';
import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Cairo from 'cairo';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';

const IRIS_BIN = '/usr/bin/iris';
const SETTINGS_BIN = '/usr/bin/iris-settings';

/* ------------------------------------------------------------------ helpers */

/** Run a command and resolve with its stdout. Never throws; resolves null on any
 *  failure, because a status probe must never be able to break the shell. */
function runAsync(argv, cancellable) {
    return new Promise(resolve => {
        let proc;
        try {
            proc = Gio.Subprocess.new(argv, Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_SILENCE);
        } catch (e) {
            resolve(null);
            return;
        }
        proc.communicate_utf8_async(null, cancellable, (source, res) => {
            try {
                const [, stdout] = source.communicate_utf8_finish(res);
                resolve(stdout ?? null);
            } catch (e) {
                resolve(null); // cancelled or spawn failure — both are "no data"
            }
        });
    });
}

/** Parse the last JSON object printed on stdout. The CLI emits one object per
 *  line; the final line is the authoritative result. */
function lastJson(text) {
    if (!text)
        return null;
    try {
        return JSON.parse(text.trim());
    } catch (e) { /* fall through to newline-delimited progress output */ }
    const lines = text.split('\n').map(l => l.trim()).filter(l => l.startsWith('{'));
    for (let i = lines.length - 1; i >= 0; i--) {
        try {
            return JSON.parse(lines[i]);
        } catch (e) { /* keep looking */ }
    }
    return null;
}

/* ------------------------------------------------- the motion spec, in numbers
 *
 * Everything below is a literal transcription of docs/ANIMATION.md. Nothing here
 * is a pixel count: lengths are fractions of R, R is a fraction of the widget's
 * shorter side, and times are milliseconds of wall clock. Changing the widget
 * size or the display scale changes nothing about how this looks.
 */

const TICK_COUNT = 48;              // N
const RADIUS_RATIO = 0.42;          // R = 0.42 * min(width, height)
const TICK_LEN_BASE = 0.085;        // L, as a fraction of R
const TICK_LEN_PEAK = 0.16;         // the longest a tick may ever be drawn
const TICK_WIDTH_RATIO = 0.028;     // W = max(1.5, 0.028 * R)
const TICK_WIDTH_MIN = 1.5;
const INNER_RATIO = 0.86;           // the ring's inner box sits at 0.86 * R

// The travelling wave (idle and scanning).
const IDLE_LAP_MS = 2600;
const SCAN_LAP_MS = 1600;
const WAVE_SIGMA = 0.10;            // gaussian width in turns — about 10% of the ring
const WAVE_GAIN = 0.52;             // opacity added at the wave head
const WAVE_STRETCH = 0.55;          // extra tick length at the wave head
const SCAN_BASE_OPACITY = 0.40;     // idle's base comes from the tick-idle token
const BREATH_MS = 1400;
const BREATH_AMPLITUDE = 0.015;     // scale 1.0 -> 1.015 -> 1.0

// Success: three overlapping beats, 720 ms end to end.
const SUCCESS_TOTAL_MS = 720;
const CONVERGE_MS = 180;            // 0 -> 180   ticks ease to uniform, wave stops
const SPRING_START_MS = 120;        // 120 -> 420 ring scale overshoot
const SPRING_END_MS = 420;
const SPRING_SCALE = 0.06;          // 1.0 -> 1.06
const SPRING_OVERSHOOT = 1.7;       // ease-out-back "c1"
// Fraction of the beat spent swelling, the rest settling back. The spec writes
// the beat as "1.0 -> 1.06 -> 1.0", which reads as symmetric, and widgets.py's
// DIAL_SPRING_RISE has to carry the same number: at 0.4 here and 0.5 there the
// pop peaked 30 ms apart in the two runtimes — close enough to look like a bug,
// and far enough to be seen with the greeter and the wizard side by side.
const SPRING_RISE = 0.5;
const CHECK_START_MS = 260;         // 260 -> 720 the checkmark draws itself
const CHECK_END_MS = 720;
const SUCCESS_TICK_LEN = 1.5;       // converged length, as a multiple of L
const CHECK_TICK_FADE = 0.35;       // ticks fade to this so the check reads

// Checkmark, normalised to the ring's inner box (unit square, origin top-left).
const CHECK_POINTS = [[0.26, 0.52], [0.44, 0.70], [0.75, 0.32]];

/* How long to hold the unlock open after a silent PAM success, so the success
 * beat is actually seen. Without this the shell dismisses the shield the instant
 * pam_iris returns and the 720ms checkmark plays to an empty screen.
 *
 * This delays YOUR OWN unlock, so it is deliberately small, capped, and only
 * applied when nothing was typed. Every failure path calls through immediately —
 * a bug here must never be able to strand someone on the lock screen. */
const SUCCESS_HOLD_MAX_MS = 3000;
const CHECK_SPLIT = 0.38;           // the short leg gets 38% of the draw progress
const CHECK_WIDTH = 0.075;          // stroke width, as a fraction of the box side

// Failure: not alarming. No red, no cross.
const SHAKE_MS = 420;
const SHAKE_AMPLITUDE = 0.035;      // A = 0.035 * R
const SHAKE_CYCLES = 3;
const DESATURATE_MS = 300;

// Progress (enrolment only — the lock screen has nothing to report a fraction of,
// but the drawing code implements it so both runtimes stay in sync).
const PROGRESS_TWEEN_MS = 260;
const PROGRESS_TICK_LEN = 1.5;
const PROGRESS_HEAD_SIGMA = 0.05;   // turns; the soft glow around the fill head

const CROSSFADE_MS = 200;           // generic state cross-fade, ease-out-quad
const REDUCED_FADE_MS = 200;        // the single fade allowed under reduced motion

// ~62 Hz. Deliberately a plain timeout rather than a Clutter transition: the dial
// is a continuous simulation, not a tween between two property values, and a
// timeout is the only thing we can prove we removed in destroy().
const FRAME_INTERVAL_MS = 16;

// Below one step of an 8-bit alpha channel there is nothing on screen to draw.
const ALPHA_EPSILON = 1 / 255;

/* Colour tokens, straight from ANIMATION.md. The lock screen is always dark, so
 * that is the default, but both columns are here because the same widget is
 * useful in a light session and a half-implemented palette rots. */
const PALETTE_DARK = {
    tickIdle:   [1.0000, 1.0000, 1.0000, 0.28],
    tickActive: [0.4706, 0.7451, 1.0000, 1.00],  // rgb(120,190,255)
    tickMuted:  [1.0000, 1.0000, 1.0000, 0.22],
    accent:     [0.4706, 0.7451, 1.0000, 1.00],
    success:    [0.2039, 0.7804, 0.3490, 1.00],  // rgb(52,199,89)
};

const PALETTE_LIGHT = {
    tickIdle:   [0.0000, 0.0000, 0.0000, 0.24],
    tickActive: [0.0000, 0.4784, 1.0000, 1.00],  // rgb(0,122,255)
    tickMuted:  [0.0000, 0.0000, 0.0000, 0.20],
    accent:     [0.0000, 0.4784, 1.0000, 1.00],
    success:    [0.1569, 0.6549, 0.2706, 1.00],  // rgb(40,167,69)
};

/* The two states that share the travelling wave. Transitions between them are
 * cross-faded by parameter rather than by picture — see _enterState(). */
const WAVE_STATES = new Set(['idle', 'scanning']);
const DIAL_STATES = new Set(['idle', 'scanning', 'progress', 'success', 'failure']);

/* --------------------------------------------------------------------- easing */

function clamp01(t) {
    return t < 0 ? 0 : (t > 1 ? 1 : t);
}

function lerp(a, b, t) {
    return a + (b - a) * t;
}

function easeOutQuad(t) {
    t = clamp01(t);
    const u = 1 - t;
    return 1 - u * u;
}

function easeOutCubic(t) {
    t = clamp01(t);
    const u = 1 - t;
    return 1 - u * u * u;
}

/** Classic ease-out-back. Overshoots past 1 near the end, then settles on it. */
function easeOutBack(t, overshoot) {
    t = clamp01(t);
    const c1 = overshoot;
    const c3 = c1 + 1;
    const u = t - 1;
    return 1 + c3 * u * u * u + c1 * u * u;
}

/** Shortest distance between two positions on a circle, measured in turns. */
function wrappedTurns(a, b) {
    const d = Math.abs(a - b) % 1;
    return d > 0.5 ? 1 - d : d;
}

/** The success ring pulse: up with an overshoot, then back to rest.
 *  Continuous at the seam — easeOutBack(1) and the start of the decay are both 1. */
function springPulse(u) {
    if (u <= 0 || u >= 1)
        return 0;
    if (u < SPRING_RISE)
        return easeOutBack(u / SPRING_RISE, SPRING_OVERSHOOT);
    return 1 - easeOutCubic((u - SPRING_RISE) / (1 - SPRING_RISE));
}

/* ----------------------------------------------------------------- the dial */

/* Per-tick appearance is kept in one flat Float64Array rather than an array of
 * objects: at 48 ticks and 60 frames a second that would be nearly three thousand
 * short-lived objects every second, and the GC pauses from that are visible as
 * stutter in exactly the motion we are trying to keep smooth. */
const TICK_STRIDE = 5;              // alpha, lengthFactor, r, g, b

const IrisDial = GObject.registerClass(
class IrisDial extends St.DrawingArea {
    /**
     * @param {object} params - St.DrawingArea construct properties, plus:
     *   `dark` (default true) to pick the ANIMATION.md colour column.
     */
    _init(params = {}) {
        const {dark = true, ...actorParams} = params;

        super._init({
            style_class: 'iris-dial',
            reactive: false,
            ...actorParams,
        });

        this._palette = dark ? PALETTE_DARK : PALETTE_LIGHT;

        this._ticks = new Float64Array(TICK_COUNT * TICK_STRIDE);
        this._snapshot = new Float64Array(TICK_COUNT * TICK_STRIDE);

        this._state = 'idle';
        this._prevState = 'idle';
        this._blendFromSnapshot = false;
        this._timerId = 0;
        this._destroyed = false;

        // "live" means an authentication attempt is genuinely in progress behind
        // this widget. When it is not, idle and scanning render as their static
        // even-ring form. That is both honest — a travelling wave asserts that
        // something is looking at the camera, and after the PAM attempt window
        // closes nothing is — and the difference between a lock screen that
        // wakes the CPU sixty times a second forever and one that does not.
        this._live = true;

        const now = GLib.get_monotonic_time();
        this._stateStartUs = now;

        // The wave head's position is derived from an origin timestamp, not
        // integrated frame by frame, so it cannot drift and it is exact whatever
        // the refresh rate. _setLap() re-anchors the origin when the speed
        // changes so the head keeps its place instead of teleporting.
        this._lapMs = IDLE_LAP_MS;
        this._phaseOriginUs = now;

        this._progressStartUs = now;
        this._progressFrom = 0;
        this._progressTo = 0;

        // Reduced motion. St.Settings mirrors this key, but the brief asks for
        // the source of truth, and reading it directly also means the widget
        // works unchanged outside the shell (in a test harness, for instance).
        this._reduced = false;
        this._ifaceSettings = null;
        this._animChangedId = 0;
        try {
            this._ifaceSettings = new Gio.Settings({schema_id: 'org.gnome.desktop.interface'});
            this._reduced = !this._ifaceSettings.get_boolean('enable-animations');
            this._animChangedId = this._ifaceSettings.connect('changed::enable-animations', () => {
                this._reduced = !this._ifaceSettings.get_boolean('enable-animations');
                this._syncTimer();
                this.queue_repaint();
            });
        } catch (e) {
            // A missing core schema should degrade to "animate", not to a broken
            // lock screen.
            this._ifaceSettings = null;
        }

        // Seed the tick buffer with the resting idle ring so the very first
        // transition cross-fades from something real rather than from nothing.
        this._fillTicks(this._ticks, now);

        this._repaintId = this.connect('repaint', () => this._onRepaint());

        // An unmapped actor cannot be seen, so animating one is pure waste. This
        // is also what stops the timer when the shell hides the lock screen
        // without destroying it.
        this._mappedId = this.connect('notify::mapped', () => this._syncTimer());

        // Teardown hangs off the 'destroy' *signal*, not a destroy() override:
        // when a parent container is destroyed its children are torn down from C,
        // which does not call a JS method override. A 60 Hz timeout that survives
        // an unlock is precisely the failure this guards against.
        this.connect('destroy', () => this._onDestroy());

        this._syncTimer();
    }

    /* ------------------------------------------------------------- public API */

    get state() {
        return this._state;
    }

    /**
     * Move to one of the five ANIMATION.md states: idle, scanning, progress,
     * success, failure. Unknown names are ignored rather than thrown, because
     * this is called from the lock-screen path where an exception is a wedged
     * session.
     */
    setState(name) {
        if (this._destroyed || !DIAL_STATES.has(name) || name === this._state)
            return;
        this._enterState(name, GLib.get_monotonic_time());
        this._syncTimer();
        this.queue_repaint();
    }

    /**
     * Declare whether something is actually happening behind the dial. See the
     * comment on this._live: when false, the wave states hold a static ring and
     * the repaint timer stops.
     */
    setLive(live) {
        live = !!live;
        if (this._destroyed || live === this._live)
            return;
        this._live = live;
        this._syncTimer();
        this.queue_repaint();
    }

    /**
     * Enrolment progress, 0..1. Tweened over 260 ms with ease-out-cubic so a
     * burst of samples reads as one continuous fill rather than a stutter.
     * Unused on the lock screen — there is no fraction there to be honest about —
     * but the state is fully implemented so both runtimes draw the same dial.
     */
    setProgress(value) {
        if (this._destroyed)
            return;
        const target = clamp01(value);
        const now = GLib.get_monotonic_time();
        this._progressFrom = this._progressValue(now);
        this._progressTo = target;
        this._progressStartUs = now;
        this._syncTimer();
        this.queue_repaint();
    }

    /* ------------------------------------------------------------ state clock */

    _elapsedMs(now) {
        return (now - this._stateStartUs) / 1000;
    }

    _enterState(name, now) {
        // Freeze the ring exactly as it was last drawn, so whatever comes next
        // eases out of the real picture instead of cutting to a new one.
        this._snapshot.set(this._ticks);

        const bothWaves = WAVE_STATES.has(name) && WAVE_STATES.has(this._state);

        this._prevState = this._state;
        this._state = name;
        this._stateStartUs = now;

        // idle <-> scanning share one wave. Cross-fading their *parameters* keeps
        // the travelling head continuous; cross-fading two rendered rings would
        // draw a ghost of the old head alongside the new one.
        this._blendFromSnapshot = !bothWaves;

        if (name === 'scanning')
            this._setLap(SCAN_LAP_MS, now);
        else if (name === 'idle' || name === 'progress')
            this._setLap(IDLE_LAP_MS, now);
        // success and failure freeze the wave; the lap is left alone so that
        // falling back to idle resumes rather than restarts.
    }

    _setLap(lapMs, now) {
        if (lapMs === this._lapMs)
            return;
        const phase = this._phaseAt(now);
        this._lapMs = lapMs;
        // Re-anchor: solve phase = (now - origin) / lap for origin.
        this._phaseOriginUs = now - phase * lapMs * 1000;
    }

    _phaseAt(now) {
        const turns = (now - this._phaseOriginUs) / 1000 / this._lapMs;
        return turns - Math.floor(turns);
    }

    _progressValue(now) {
        const t = easeOutCubic((now - this._progressStartUs) / 1000 / PROGRESS_TWEEN_MS);
        return lerp(this._progressFrom, this._progressTo, t);
    }

    /** True while the travelling wave should actually travel. */
    _waveMoves() {
        return !this._reduced && this._live;
    }

    _successDurationMs() {
        return this._reduced ? REDUCED_FADE_MS : SUCCESS_TOTAL_MS;
    }

    _failureDurationMs() {
        return this._reduced ? REDUCED_FADE_MS : SHAKE_MS;
    }

    _isAnimating(now) {
        if (this._blendFromSnapshot && this._elapsedMs(now) < CROSSFADE_MS)
            return true;

        switch (this._state) {
        case 'success':
            return this._elapsedMs(now) < this._successDurationMs();
        case 'failure':
            // Always true: _onFrame hands failure back to idle once its beats are
            // done, and that handover needs one more frame to happen in.
            return true;
        case 'progress':
            return (now - this._progressStartUs) / 1000 < PROGRESS_TWEEN_MS || this._waveMoves();
        default:
            return this._waveMoves();
        }
    }

    /* ----------------------------------------------------------- frame source */

    _syncTimer() {
        if (this._destroyed)
            return;
        const wanted = this.mapped && this._isAnimating(GLib.get_monotonic_time());
        if (wanted && !this._timerId) {
            this._timerId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, FRAME_INTERVAL_MS,
                () => this._onFrame());
        } else if (!wanted && this._timerId) {
            GLib.Source.remove(this._timerId);
            this._timerId = 0;
        }
    }

    _onFrame() {
        // A throw out of a GSource callback is reported by gjs and the source is
        // dropped — but `_timerId` would still hold its id. Two things go wrong
        // then, and both outlive the frame: _syncTimer() sees a non-zero id and
        // believes a timer is already running, so the dial never restarts; and
        // _onDestroy() calls GLib.Source.remove() on an id that no longer exists,
        // which is a G_CRITICAL on the unlock path. So the id is cleared here on
        // any failure, by the same code that owns it.
        try {
            const now = GLib.get_monotonic_time();

            // ANIMATION.md, failure: "Then fall back to idle so the user can
            // simply try again." Done from the frame we already have rather than
            // with a second timeout — one source means one thing to remove.
            if (this._state === 'failure' && this._elapsedMs(now) >= this._failureDurationMs())
                this._enterState('idle', now);

            this.queue_repaint();

            if (!this._isAnimating(now)) {
                // The repaint queued above still lands, so the last frame of a
                // finished animation is drawn before the source goes away.
                this._timerId = 0;
                return GLib.SOURCE_REMOVE;
            }
            return GLib.SOURCE_CONTINUE;
        } catch (e) {
            this._timerId = 0;
            return GLib.SOURCE_REMOVE;
        }
    }

    /* -------------------------------------------------------- tick appearance */

    /**
     * Resolve every tick's alpha, length multiplier and colour for this instant
     * into `out`. Written as one pass over a flat buffer so a frame allocates
     * nothing at all.
     */
    _fillTicks(out, now) {
        switch (this._state) {
        case 'success':
            this._fillSuccess(out, now);
            break;
        case 'failure':
            this._fillFailure(out, now);
            break;
        case 'progress':
            this._fillProgress(out, now);
            break;
        default:
            this._fillWave(out, now);
            break;
        }

        // Generic 200 ms ease-out-quad cross-fade for transitions the spec does
        // not give its own beats for (success/failure -> idle, anything ->
        // progress). success and failure consume the snapshot themselves.
        if (this._blendFromSnapshot && this._state !== 'success' && this._state !== 'failure') {
            const k = easeOutQuad(this._elapsedMs(now) / CROSSFADE_MS);
            if (k < 1)
                this._blendInto(out, this._snapshot, k);
        }
    }

    /** out = lerp(from, out, k), applied uniformly to alpha, length and colour. */
    _blendInto(out, from, k) {
        for (let i = 0; i < out.length; i++)
            out[i] = from[i] + (out[i] - from[i]) * k;
    }

    /** idle and scanning: one travelling gaussian wave around the ring. */
    _fillWave(out, now) {
        const p = this._palette;
        const idleBase = p.tickIdle[3];
        const moving = this._waveMoves();

        // "brighter (base 0.40)" is the only parameter that differs between the
        // two wave states once the lap length is handled by the phase origin, so
        // the cross-fade is a single scalar.
        let base = this._state === 'scanning' ? SCAN_BASE_OPACITY : idleBase;
        if (WAVE_STATES.has(this._prevState) && WAVE_STATES.has(this._state)) {
            const k = easeOutQuad(this._elapsedMs(now) / CROSSFADE_MS);
            if (k < 1) {
                const from = this._prevState === 'scanning' ? SCAN_BASE_OPACITY : idleBase;
                base = lerp(from, base, k);
            }
        }

        const head = this._phaseAt(now);

        for (let i = 0; i < TICK_COUNT; i++) {
            let boost = 0;
            if (moving) {
                // ANIMATION.md: boost(i) = exp(-(d(i) / 0.10)^2)
                const x = wrappedTurns(i / TICK_COUNT, head) / WAVE_SIGMA;
                boost = Math.exp(-x * x);
            }
            // Reduced motion (or nothing live): no wave. Idle becomes a dim even
            // ring and scanning a brighter one, exactly as the spec requires —
            // the information is in the brightness, not in the movement.

            const o = i * TICK_STRIDE;
            out[o] = Math.min(1, base + WAVE_GAIN * boost);
            out[o + 1] = 1 + WAVE_STRETCH * boost;
            out[o + 2] = lerp(p.tickIdle[0], p.tickActive[0], boost);
            out[o + 3] = lerp(p.tickIdle[1], p.tickActive[1], boost);
            out[o + 4] = lerp(p.tickIdle[2], p.tickActive[2], boost);
        }
    }

    /** progress: ticks below the head filled in the accent, the head glowing. */
    _fillProgress(out, now) {
        const p = this._palette;
        const head = this._progressValue(now);

        // Above the head the ring keeps doing whatever idle does, so start there.
        this._fillWave(out, now);

        for (let i = 0; i < TICK_COUNT; i++) {
            const o = i * TICK_STRIDE;
            const pos = i / TICK_COUNT;

            if (pos < head) {
                out[o] = 1;
                out[o + 1] = PROGRESS_TICK_LEN;
                out[o + 2] = p.accent[0];
                out[o + 3] = p.accent[1];
                out[o + 4] = p.accent[2];
            }

            // A soft glow either side of the head, so the boundary reads as a
            // moving edge rather than as a step. Skipped at zero: with no
            // samples captured the head is at twelve o'clock, and lighting it
            // would claim progress that has not happened. The GTK4 dial skips
            // its head glow at zero for the same reason.
            if (head <= 0)
                continue;

            const x = wrappedTurns(pos, head) / PROGRESS_HEAD_SIGMA;
            const glow = Math.exp(-x * x);
            if (glow > ALPHA_EPSILON) {
                out[o] = Math.min(1, out[o] + 0.35 * glow);
                out[o + 1] += 0.25 * glow;
                out[o + 2] = lerp(out[o + 2], p.accent[0], glow);
                out[o + 3] = lerp(out[o + 3], p.accent[1], glow);
                out[o + 4] = lerp(out[o + 4], p.accent[2], glow);
            }
        }
    }

    /** success beats 1 and 3: converge to a uniform ring, then fade under the check. */
    _fillSuccess(out, now) {
        const p = this._palette;
        const elapsed = this._elapsedMs(now);

        // Beat 3 fades the ticks back so the checkmark reads clearly. Under
        // reduced motion the ring is simply already there at its faded value —
        // the only thing allowed to change is the check's single 200 ms fade.
        let alpha;
        if (this._reduced) {
            alpha = CHECK_TICK_FADE;
        } else if (elapsed <= CHECK_START_MS) {
            alpha = 1;
        } else {
            const k = easeOutCubic((elapsed - CHECK_START_MS) / (CHECK_END_MS - CHECK_START_MS));
            alpha = lerp(1, CHECK_TICK_FADE, k);
        }

        for (let i = 0; i < TICK_COUNT; i++) {
            const o = i * TICK_STRIDE;
            out[o] = alpha;
            out[o + 1] = SUCCESS_TICK_LEN;
            out[o + 2] = p.success[0];
            out[o + 3] = p.success[1];
            out[o + 4] = p.success[2];
        }

        // Beat 1, 0 -> 180 ms: every tick eases from wherever the wave left it to
        // that uniform ring. This *is* the state cross-fade for success, which is
        // why there is no separate one.
        if (!this._reduced) {
            const converge = easeOutCubic(elapsed / CONVERGE_MS);
            if (converge < 1)
                this._blendInto(out, this._snapshot, converge);
        }
    }

    /** failure: desaturate to the muted token and return to base length. */
    _fillFailure(out, now) {
        const p = this._palette;
        // Reduced motion gets the same destination, reached by a plain fade:
        // "failure dims once".
        const span = this._reduced ? REDUCED_FADE_MS : DESATURATE_MS;
        const k = this._reduced
            ? easeOutQuad(this._elapsedMs(now) / span)
            : easeOutCubic(this._elapsedMs(now) / span);

        for (let i = 0; i < TICK_COUNT; i++) {
            const o = i * TICK_STRIDE;
            out[o] = p.tickMuted[3];
            out[o + 1] = 1;
            out[o + 2] = p.tickMuted[0];
            out[o + 3] = p.tickMuted[1];
            out[o + 4] = p.tickMuted[2];
        }

        if (k < 1)
            this._blendInto(out, this._snapshot, k);
    }

    /* --------------------------------------------------------- whole-ring pose */

    /**
     * The transform applied to the ring as a body: `scale` about its centre and
     * `dx` sideways, expressed in units of R so it scales with the widget.
     */
    _pose(now) {
        if (this._reduced)
            return {scale: 1, dx: 0};   // no breathing, no spring, no shake

        const elapsed = this._elapsedMs(now);

        if (this._state === 'scanning' && this._live) {
            // 1.0 -> 1.015 -> 1.0 on a 1400 ms sine, starting and ending at rest.
            const u = (elapsed % BREATH_MS) / BREATH_MS;
            const scale = 1 + BREATH_AMPLITUDE * 0.5 * (1 - Math.cos(2 * Math.PI * u));
            return {scale, dx: 0};
        }

        if (this._state === 'success') {
            const u = (elapsed - SPRING_START_MS) / (SPRING_END_MS - SPRING_START_MS);
            return {scale: 1 + SPRING_SCALE * springPulse(u), dx: 0};
        }

        if (this._state === 'failure') {
            // x = A * sin(2*pi*3*u) * (1-u): three oscillations, damped to zero,
            // so it settles rather than stopping dead.
            const u = clamp01(elapsed / SHAKE_MS);
            const dx = SHAKE_AMPLITUDE * Math.sin(2 * Math.PI * SHAKE_CYCLES * u) * (1 - u);
            return {scale: 1, dx};
        }

        return {scale: 1, dx: 0};
    }

    /** How much of the checkmark is drawn, 0..1, and at what opacity. */
    _checkDraw(now) {
        if (this._state !== 'success')
            return null;
        const elapsed = this._elapsedMs(now);

        if (this._reduced) {
            // "success draws the checkmark with a single 200 ms fade"
            return {progress: 1, alpha: easeOutQuad(elapsed / REDUCED_FADE_MS)};
        }
        if (elapsed < CHECK_START_MS)
            return null;
        const progress = easeOutCubic((elapsed - CHECK_START_MS) / (CHECK_END_MS - CHECK_START_MS));
        return {progress, alpha: 1};
    }

    /* -------------------------------------------------------------- rendering */

    _onRepaint() {
        const [width, height] = this.get_surface_size();
        if (width <= 0 || height <= 0)
            return;

        const cr = this.get_context();
        try {
            this._draw(cr, width, height);
        } finally {
            // GJS will not collect a cairo context on its own in any useful
            // timeframe; the shell leaks memory steadily without this.
            cr.$dispose();
        }
    }

    _draw(cr, width, height) {
        const now = GLib.get_monotonic_time();

        // Every constant below is derived from the real allocation, which is what
        // makes this resolution independent: St hands us a context already scaled
        // for the display, so logical units here are physically crisp at 2x.
        const side = Math.min(width, height);
        const R = RADIUS_RATIO * side;
        const L = TICK_LEN_BASE * R;
        const maxLen = TICK_LEN_PEAK * R;
        const lineWidth = Math.max(TICK_WIDTH_MIN, TICK_WIDTH_RATIO * R);

        const ticks = this._ticks;
        this._fillTicks(ticks, now);

        const {scale, dx} = this._pose(now);

        cr.save();
        cr.translate(width / 2 + dx * R, height / 2);
        if (scale !== 1)
            cr.scale(scale, scale);

        cr.setLineWidth(lineWidth);
        cr.setLineCap(Cairo.LineCap.ROUND);
        cr.setLineJoin(Cairo.LineJoin.ROUND);

        for (let i = 0; i < TICK_COUNT; i++) {
            const o = i * TICK_STRIDE;
            const alpha = ticks[o];
            if (alpha <= ALPHA_EPSILON)
                continue;

            const len = Math.min(L * ticks[o + 1], maxLen);

            // Index 0 is twelve o'clock and the ring runs clockwise; screen Y
            // grows downwards, so this is the ordinary positive sweep.
            const angle = (i / TICK_COUNT) * 2 * Math.PI - Math.PI / 2;
            const cos = Math.cos(angle);
            const sin = Math.sin(angle);

            // Ticks are centred on R, not grown outward from it, so a lengthening
            // tick reads as a mark thickening in place rather than as the whole
            // ring swelling. R + peak length + a round cap still clears the
            // widget edge under the 1.066 success overshoot — which is what the
            // spec's R = 0.42 buys.
            const inner = R - len / 2;
            const outer = R + len / 2;

            cr.setSourceRGBA(ticks[o + 2], ticks[o + 3], ticks[o + 4], alpha);
            cr.moveTo(cos * inner, sin * inner);
            cr.lineTo(cos * outer, sin * outer);
            cr.stroke();
        }

        cr.restore();

        // The checkmark is drawn OUTSIDE the ring's pose on purpose, and the
        // GTK4 dial does the same. Beats two and three overlap between 260 ms
        // and 420 ms, so a mark that rode the spring would visibly wobble as it
        // was being drawn — and it is the one element the eye is tracking
        // stroke by stroke. The ring springs; the thing it reveals holds still.
        const check = this._checkDraw(now);
        if (check) {
            cr.save();
            cr.translate(width / 2, height / 2);
            this._drawCheck(cr, R, check.progress, check.alpha);
            cr.restore();
        }
    }

    /**
     * The checkmark, specified in ANIMATION.md as a unit square over the ring's
     * inner box. Drawn as one continuous stroke — two separate strokes would meet
     * at a visible seam even with round caps.
     */
    _drawCheck(cr, R, progress, alpha) {
        if (progress <= ALPHA_EPSILON || alpha <= ALPHA_EPSILON)
            return;

        const box = 2 * INNER_RATIO * R;    // the inner circle's bounding square
        const origin = -box / 2;            // unit-square origin, top-left
        const px = i => origin + CHECK_POINTS[i][0] * box;
        const py = i => origin + CHECK_POINTS[i][1] * box;

        const p = this._palette.success;
        cr.setSourceRGBA(p[0], p[1], p[2], alpha);
        cr.setLineWidth(CHECK_WIDTH * box);
        cr.setLineCap(Cairo.LineCap.ROUND);
        cr.setLineJoin(Cairo.LineJoin.ROUND);

        cr.newPath();
        cr.moveTo(px(0), py(0));

        if (progress <= CHECK_SPLIT) {
            // The short leg is geometrically only ~34% of the path but is given
            // 38% of the time, so it does not look rushed against the long one.
            const t = progress / CHECK_SPLIT;
            cr.lineTo(lerp(px(0), px(1), t), lerp(py(0), py(1), t));
        } else {
            cr.lineTo(px(1), py(1));
            const t = (progress - CHECK_SPLIT) / (1 - CHECK_SPLIT);
            cr.lineTo(lerp(px(1), px(2), t), lerp(py(1), py(2), t));
        }
        cr.stroke();
    }

    /* --------------------------------------------------------------- teardown */

    _onDestroy() {
        this._destroyed = true;

        if (this._timerId) {
            GLib.Source.remove(this._timerId);
            this._timerId = 0;
        }
        if (this._animChangedId && this._ifaceSettings) {
            this._ifaceSettings.disconnect(this._animChangedId);
            this._animChangedId = 0;
        }
        this._ifaceSettings = null;

        if (this._repaintId) {
            this.disconnect(this._repaintId);
            this._repaintId = 0;
        }
        if (this._mappedId) {
            this.disconnect(this._mappedId);
            this._mappedId = 0;
        }

        this._ticks = null;
        this._snapshot = null;
    }
});

/* ----------------------------------------------- the shell's own auth prompt
 *
 * Everything below reaches into GNOME Shell's private structure, so it states
 * up front what it is relying on. Verified against gnome-shell 50.1 by reading
 * the JS out of the GResource embedded in /usr/lib/gnome-shell/libshell-18.so:
 *
 *   gresource extract /usr/lib/gnome-shell/libshell-18.so \
 *       /org/gnome/shell/gdm/authPrompt.js
 *
 *   - Main.screenShield._dialog is the only handle on the dialog. ScreenShield
 *     exposes `locked` and `active` and nothing else; there is no accessor.
 *   - In `user` mode that field holds an UnlockDialog. In `gdm` mode it holds a
 *     LoginDialog, because sessionMode's gdm entry sets `unlockDialog:
 *     LoginDialog`. The chain to the prompt is the same either way.
 *   - AuthPrompt is a stock vertical St.BoxLayout. It defines vfunc_hide and
 *     nothing else, so a child added to it is measured and allocated normally.
 *     Both dialogs size the auth column from the prompt's get_preferred_size(),
 *     so a prompt that grows to fit the dial stays centred by itself.
 *
 * The two things that are NOT relied on, because they do not exist here:
 *   - AuthPrompt has no 'verification-started' / 'verification-complete' /
 *     'verification-failed' GObject signals. Those names live on
 *     ShellUserVerifier, which is a plain JS EventEmitter. The AuthPrompt's
 *     real state machine is the `verification-status` property, and that is
 *     what drives the dial.
 *   - Main.layoutManager.modalDialogGroup is NOT the lock screen's parent. The
 *     dialog lives in ScreenShield._lockDialogGroup, which is translated off
 *     screen while the curtain is up — which is exactly why absolutely
 *     positioning a hint in modalDialogGroup drew nothing.
 */

/* Transcribed from AuthPromptStatus in resource:///org/gnome/shell/gdm/authPrompt.js.
 *
 * Deliberately a local copy rather than an import of that module. A static ESM
 * import cannot be caught, so a module that failed to resolve would stop the
 * whole extension from loading — including the top-bar indicator, which needs
 * none of this. The values are only ever compared against a number we read off
 * a live prompt, so the worst a drift could do is leave the dial in its idle
 * state. */
const AuthPromptStatus = {
    NOT_VERIFYING: 0,
    VERIFYING: 1,
    VERIFICATION_FAILED: 2,
    VERIFICATION_SUCCEEDED: 3,
    VERIFICATION_CANCELLED: 4,
    VERIFICATION_IN_PROGRESS: 5,
};

/* AuthPrompt's own children, in order: [0] _userWell (the avatar), then
 * [1] _mainContent (the entry row, the caps-lock warning and the PAM message
 * label). Index 1 puts the dial under the face and above the field.
 *
 * It also survives the one thing that rearranges the prompt: setAuthBlocked()
 * swaps _mainContent for a ParentalControlsShield with replace_child(), which
 * preserves the sibling index. Inserting *inside* _inputWell instead would
 * vanish with it. */
const HINT_CHILD_INDEX = 1;

/* --------------------------------------------------------------- lock hint  */

/* The pill shown between the avatar and the password entry: the dial, plus a
 * line of text.
 *
 * The text is the part that has to be careful. This extension does not decide
 * authentication and cannot see the camera, so the pill may only say things it
 * has actually observed:
 *
 *   - `scanning`, while the prompt reports VERIFYING: begin() has handed the
 *     conversation to PAM, so pam_iris.so is running and the daemon is looking
 *     at the camera right now. "Looking for your face" is a fact. It explicitly
 *     does NOT mean a face has been detected; there is no way to know that.
 *   - `checking`, once the prompt reports VERIFICATION_IN_PROGRESS: the user
 *     submitted an answer, so whatever happens next is about that answer.
 *   - `waiting`, once the configured attempt window has elapsed with no verdict:
 *     face unlock is not going to happen on its own, so say so.
 *   - `failed`, on VERIFICATION_FAILED: PAM is finished and did not let anyone
 *     in. The dial's failure beat is deliberately undramatic — a desaturate and
 *     a small shake, no red and no cross.
 *   - `success` is the one verdict that needs an attribution as well as a
 *     result, and IrisPromptBinding is the only thing allowed to ask for it.
 *     See the comment there.
 *
 * Screen readers get the state from the label, which changes with it; the dial
 * itself is decorative and deliberately exposes no accessible text of its own,
 * so nothing is announced twice.
 */

const HINT_FADE_MS = 400;

const HINT_STATE_CLASSES = ['iris-state-idle', 'iris-state-success'];

const HINT_PHASES = {
    scanning: {dial: 'scanning', live: true,  klass: null,                 text: 'Looking for your face…'},
    checking: {dial: 'idle',     live: false, klass: 'iris-state-idle',    text: 'Checking…'},
    waiting:  {dial: 'idle',     live: false, klass: 'iris-state-idle',    text: 'Enter your password'},
    failed:   {dial: 'failure',  live: false, klass: 'iris-state-idle',    text: 'Sign-in failed'},
    success:  {dial: 'success',  live: false, klass: 'iris-state-success', text: 'Unlocked'},
};

const IrisHint = GObject.registerClass(
class IrisHint extends St.BoxLayout {
    _init() {
        super._init({
            style_class: 'iris-lock-hint',
            orientation: Clutter.Orientation.HORIZONTAL,
            x_align: Clutter.ActorAlign.CENTER,
            y_align: Clutter.ActorAlign.CENTER,
            x_expand: true,
            reactive: false,
            // Hidden, not merely transparent. A zero-opacity actor is still
            // allocated, and an invisible 80px hole above the password field
            // would push the whole prompt down for an attempt that may never
            // start. Hidden children get no allocation at all, so until there
            // is something true to say the prompt looks exactly as it always
            // did.
            visible: false,
            opacity: 0,
        });

        this._dial = new IrisDial({
            y_align: Clutter.ActorAlign.CENTER,
            dark: true,   // the lock screen and the greeter are always dark
        });
        this._label = new St.Label({
            style_class: 'iris-hint-label',
            text: HINT_PHASES.scanning.text,
            y_align: Clutter.ActorAlign.CENTER,
        });
        this.add_child(this._dial);
        this.add_child(this._label);

        this._phase = null;
        this._revealed = false;
    }

    get phase() {
        return this._phase;
    }

    /** Apply one of HINT_PHASES. Unknown names are ignored rather than thrown:
     *  this runs on the unlock path, where an exception is a wedged session. */
    setPhase(name) {
        const phase = HINT_PHASES[name];
        if (!phase || !this._dial || !this._label || name === this._phase)
            return;

        this._phase = name;

        for (const klass of HINT_STATE_CLASSES) {
            if (klass === phase.klass)
                this.add_style_class_name(klass);
            else
                this.remove_style_class_name(klass);
        }

        this._label.text = phase.text;
        this._dial.setState(phase.dial);
        this._dial.setLive(phase.live);

        // Only `scanning` may bring the pill on screen, because it is the only
        // phase backed by a PAM conversation we watched open. Everything before
        // that — the prompt being constructed, a reset on its way to begin() —
        // leaves the prompt untouched.
        if (name === 'scanning')
            this._reveal();
    }

    _reveal() {
        if (this._revealed)
            return;
        this._revealed = true;

        this.show();
        // ease() honours org.gnome.desktop.interface enable-animations through
        // the shell's environment.js, so under reduced motion this is a cut.
        this.ease({
            opacity: 255,
            duration: HINT_FADE_MS,
            mode: Clutter.AnimationMode.EASE_OUT_QUAD,
        });
    }

    destroy() {
        // Stopping transitions before teardown avoids a callback firing against
        // a disposed actor, which is the classic extension crash on unlock. The
        // dial's own 60 Hz timeout and its enable-animations handler are
        // released from its 'destroy' handler, which fires whichever way it is
        // torn down — including when the C side destroys it as our child.
        this.remove_all_transitions();
        this._dial = null;
        this._label = null;
        super.destroy();
    }
});

/* ------------------------------------------------------- prompt <-> dial glue
 *
 * One of these owns one AuthPrompt: the actor it inserted, the handlers it
 * connected to that prompt, and the attempt-window timeout. Nothing else in the
 * extension touches the prompt, so "did we clean up?" has exactly one answer.
 *
 * The prompt outlives us on the greeter (LoginDialog builds it in _init and
 * only ever hides it) and is far shorter-lived than us on the lock screen
 * (UnlockDialog destroys it on every crossfade back to the clock). Both cases
 * are covered by hanging teardown off the prompt's 'destroy' signal as well as
 * off our own destroy(), because a C-side teardown does not call a JS method.
 */
class IrisPromptBinding {
    /**
     * @param {object} prompt - the shell's AuthPrompt.
     * @param {number} timeoutSeconds - the attempt window, from `hint-timeout`.
     * @param {?Function} onGone - called with this binding when the prompt is
     *   destroyed under it, so the owner can drop its reference instead of
     *   holding a spent object until the next attach.
     */
    constructor(prompt, timeoutSeconds, successHoldMs = 0, onGone = null) {
        this.prompt = prompt;
        this._timeoutSeconds = timeoutSeconds;
        this._successHoldMs = successHoldMs;
        this._onGone = onGone;
        this._hint = null;
        this._destroyId = 0;
        this._notifyId = 0;
        this._resetId = 0;
        this._timeoutId = 0;

        // Latched for the length of one PAM conversation: true once the prompt
        // has reported that the user submitted an answer. Cleared when a fresh
        // conversation opens.
        this._answered = false;
        this._showedSuccess = false;
        this._holdId = 0;
        this._pendingFinish = null;
        this._originalFinish = null;

        this._done = false;
    }

    /** @returns {boolean} true if the dial is attached and driven. */
    attach() {
        // Check the shape BEFORE putting an actor in the prompt, not after.
        //
        // g_signal_connect() on `notify::` does not verify that the detail names
        // a real property — it succeeds and simply never fires — so connecting
        // is not a test of anything. If verificationStatus is not a number then
        // this is not the AuthPrompt we verified against, we could never drive
        // the dial from it, and the right move is to leave the prompt exactly as
        // we found it rather than park a permanently invisible actor in it.
        try {
            if (typeof this.prompt?.insert_child_at_index !== 'function' ||
                typeof this.prompt.verificationStatus !== 'number') {
                console.debug('Iris: auth prompt is not the expected shape; showing nothing.');
                return false;
            }
        } catch (e) {
            return false;
        }

        try {
            const hint = new IrisHint();
            this.prompt.insert_child_at_index(hint, HINT_CHILD_INDEX);
            this._hint = hint;
        } catch (e) {
            console.debug(`Iris: could not insert the dial into the auth prompt: ${e}`);
            this._disposeHint();
            return false;
        }

        try {
            this._destroyId = this.prompt.connect('destroy',
                () => this._onPromptDestroyed());
        } catch (e) {
            this._destroyId = 0;
        }

        // The whole state machine. Every AuthPromptStatus transition assigns
        // this.verificationStatus, and gjs fires notify for each one.
        try {
            this._notifyId = this.prompt.connect('notify::verification-status',
                () => this._sync());
        } catch (e) {
            this._notifyId = 0;
        }

        // 'reset' carries a BeginRequestType and fires on the way into a retry,
        // just before the dialog calls begin() again. Re-arming here means a
        // second attempt starts from a clean slate even if the status happens
        // to land on a value it already held.
        try {
            this._resetId = this.prompt.connect('reset', () => {
                this._answered = false;
                // ScreenShield resets and restarts AuthPrompt after reporting
                // success but before it calls finish(). Keep the success latch
                // and painted checkmark until finish() consumes them; otherwise
                // the lock screen calculates a 0 ms hold and disappears at once.
                this._sync();
            });
        } catch (e) {
            this._resetId = 0;
        }

        // Without the status property there is nothing honest to drive the dial
        // with, and a ring that can never change state is worse than no ring.
        if (!this._destroyId || !this._notifyId) {
            console.debug('Iris: auth prompt is missing its expected signals; showing nothing.');
            this.destroy();
            return false;
        }

        this._wrapFinish();

        this._sync();
        return true;
    }

    /* ------------------------------------------------------- the success hold */

    /* AuthPrompt.finish(onComplete) is what the shell waits on before tearing the
     * dialog down (unlockDialog.js:997 and the greeter both route through it), so
     * it is the one honest place to buy the checkmark some time on screen.
     *
     * Safety rules, in order of importance:
     *   1. onComplete is ALWAYS called. Every catch calls through, and destroy()
     *      flushes a pending hold. Failing to call it strands the user on the
     *      lock screen, which is far worse than no animation.
     *   2. Only held when nothing was typed. A typed password unlocks at full
     *      speed -- padding that would be a pure regression.
     *   3. Capped at SUCCESS_HOLD_MAX_MS regardless of configuration.
     */
    _wrapFinish() {
        try {
            if (typeof this.prompt?.finish !== 'function')
                return;

            const original = this.prompt.finish;
            this._originalFinish = original;
            const binding = this;

            this.prompt.finish = function (onComplete) {
                const callThrough = () => {
                    // finish() owns the end of this conversation. Clear only at
                    // hand-off, not on AuthPrompt's intermediate reset signal.
                    binding._showedSuccess = false;
                    try {
                        original.call(this, onComplete);
                    } catch (e) {
                        // Even the shell's own finish() failed. Unlock anyway.
                        try {
                            onComplete();
                        } catch (_) { /* nothing left to try */ }
                    }
                };

                let hold = 0;
                try {
                    hold = binding._holdMs();
                } catch (e) {
                    hold = 0;
                }

                if (hold <= 0) {
                    callThrough();
                    return;
                }

                // Remember the continuation so destroy() can flush it if the
                // dialog goes away mid-hold.
                binding._pendingFinish = callThrough;
                binding._holdId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, hold, () => {
                    binding._holdId = 0;
                    const run = binding._pendingFinish;
                    binding._pendingFinish = null;
                    if (run)
                        run();
                    return GLib.SOURCE_REMOVE;
                });
            };
        } catch (e) {
            console.debug(`Iris: could not wrap finish(): ${e}`);
            this._originalFinish = null;
        }
    }

    /** Milliseconds to hold, or 0 for "hand over immediately". */
    _holdMs() {
        // A typed password is not ours to celebrate, and padding it is a regression.
        if (this._answered)
            return 0;
        // Gate on having actually PUT the checkmark on screen, not on the status
        // still reading SUCCEEDED here. deactivate() -> finish() can run after the
        // prompt has already moved on, and the earlier status test made the hold
        // silently never apply on the lock screen.
        if (!this._showedSuccess)
            return 0;
        let ms = this._successHoldMs ?? 0;
        if (!Number.isFinite(ms) || ms <= 0)
            return 0;
        return Math.min(ms, SUCCESS_HOLD_MAX_MS);
    }

    /** Restore finish() and make sure no unlock is left hanging. */
    _releaseFinish() {
        if (this._holdId) {
            GLib.Source.remove(this._holdId);
            this._holdId = 0;
        }
        // A hold was in flight when the prompt went away: complete it NOW.
        const pending = this._pendingFinish;
        this._pendingFinish = null;
        this._showedSuccess = false;
        if (pending) {
            try {
                pending();
            } catch (e) { /* already best-effort inside */ }
        }
        if (this._originalFinish) {
            try {
                this.prompt.finish = this._originalFinish;
            } catch (e) { /* prompt already disposed */ }
            this._originalFinish = null;
        }
    }

    /* -------------------------------------------------------- state machine */

    _sync() {
        if (this._done || !this._hint)
            return;

        // GNOME ScreenShield emits reset and starts another verifier between
        // VERIFICATION_SUCCEEDED and finish(). Preserve the completed success
        // animation through those internal transitions. finish() or teardown
        // releases this latch, so a later conversation still starts normally.
        if (this._showedSuccess)
            return;

        let status;
        try {
            status = this.prompt.verificationStatus;
        } catch (e) {
            return;
        }

        // Everything below drives actors we parked inside the shell's prompt, and
        // it runs on the shell's own emission chain: `notify::verification-status`
        // is emitted synchronously from inside AuthPrompt.begin(), and 'reset' from
        // inside AuthPrompt.reset(). gjs contains a throw from a signal handler, so
        // this cannot wedge an unlock either way — but a disposed-actor error here
        // would be logged on every transition of a live authentication, and the
        // dial would silently stop tracking the prompt. Catching it locally means
        // the worst case is one frozen decoration rather than a spent binding that
        // keeps re-throwing.
        try {
            this._applyStatus(status);
        } catch (e) {
            console.debug(`Iris: could not update the dial: ${e}`);
        }
    }

    _applyStatus(status) {
        switch (status) {
        case AuthPromptStatus.VERIFYING:
            // authPrompt.js begin(): _userVerifier.begin() has just been called,
            // so the PAM conversation is open and pam_iris.so is looking. This
            // is the one moment the extension can honestly claim the camera is
            // in use.
            this._answered = false;
            this._hint.setPhase('scanning');
            this._armTimeout();
            break;

        case AuthPromptStatus.VERIFICATION_IN_PROGRESS:
            // authPrompt.js _activateNext(): the user submitted an answer. The
            // camera is no longer the path being taken.
            this._answered = true;
            this._clearTimeout();
            this._hint.setPhase('checking');
            break;

        case AuthPromptStatus.VERIFICATION_SUCCEEDED:
            this._clearTimeout();
            // Success is an attribution as well as a result, and this is the
            // only place allowed to ask for it.
            //
            // PAM reports that the conversation succeeded. It does not report
            // which module succeeded, and the shell cannot see the camera. So a
            // green ring on the Iris dial after a typed password would be a lie
            // told by association.
            //
            // What we can observe is whether an answer was ever submitted: the
            // prompt only reaches VERIFICATION_IN_PROGRESS from _activateNext(),
            // which runs when the user hits Enter. A success that never passed
            // through it was granted without anyone typing anything — which is
            // what a silent module like pam_iris.so does. That, and only that,
            // earns the checkmark.
            if (!this._answered) {
                this._showedSuccess = true;
            }   // gates the unlock hold below
            this._hint.setPhase(this._answered ? 'checking' : 'success');
            break;

        case AuthPromptStatus.VERIFICATION_FAILED:
            // Only ever set for a terminal failure (authPrompt.js sets it from
            // _onVerificationFailed when !canRetry, and from cancel() once the
            // retries are spent), so this is not a wrong-password blip.
            this._clearTimeout();
            this._hint.setPhase('failed');
            break;

        case AuthPromptStatus.VERIFICATION_CANCELLED:
        case AuthPromptStatus.NOT_VERIFYING:
        default:
            // NOT_VERIFYING is also the construction value and the momentary
            // state inside reset(), which emits 'reset' and is followed
            // synchronously by begin(). Nothing is painted between the two, so
            // this cannot flicker, and while the pill is still unrevealed it
            // changes nothing on screen at all.
            this._clearTimeout();
            this._hint.setPhase('waiting');
            break;
        }
    }

    /* ----------------------------------------------------- the attempt window
     *
     * PAM never tells the shell "the face attempt is over, ask for a password".
     * pam_iris.so simply stops being the module that answers, and the prompt
     * sits at VERIFYING with the entry waiting. So this stays what it always
     * was: a mirror of the daemon's configured auth.timeout, an expectation
     * rather than an observation. When it elapses the pill stops implying that
     * anything is still looking. It is never allowed to declare a verdict.
     */

    _armTimeout() {
        this._clearTimeout();
        const seconds = this._timeoutSeconds;
        if (!(seconds > 0))
            return;
        this._timeoutId = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, seconds, () => {
            // Cleared first, so the id is never left pointing at a source that
            // has already returned — including down the throwing path below.
            this._timeoutId = 0;
            try {
                if (!this._done && this._hint)
                    this._hint.setPhase('waiting');
            } catch (e) { /* the pill is decoration; never take the session with it */ }
            return GLib.SOURCE_REMOVE;
        });
    }

    _clearTimeout() {
        if (this._timeoutId) {
            GLib.Source.remove(this._timeoutId);
            this._timeoutId = 0;
        }
    }

    /* --------------------------------------------------------------- teardown */

    _onPromptDestroyed() {
        // The prompt is going away — on the lock screen that is every swipe back
        // to the clock, via UnlockDialog._maybeDestroyAuthPrompt(). Our hint is
        // its child, so Clutter destroys it for us, and that is what releases
        // the dial's frame timer. Touching it here would be a double free, so
        // drop the reference and let the C side finish.
        this._destroyId = 0;
        this._notifyId = 0;
        this._resetId = 0;
        this._hint = null;

        const onGone = this._onGone;
        this.destroy();
        try {
            onGone?.(this);
        } catch (e) { /* the owner is on its own from here */ }
    }

    destroy() {
        if (this._done)
            return;
        this._done = true;
        this._onGone = null;

        // Before dropping the prompt: restore its method and never leave an
        // unlock continuation waiting on our timer.
        this._releaseFinish();
        this._clearTimeout();

        for (const field of ['_destroyId', '_notifyId', '_resetId']) {
            const id = this[field];
            this[field] = 0;
            if (!id)
                continue;
            try {
                this.prompt.disconnect(id);
            } catch (e) { /* prompt already finalised */ }
        }

        this._disposeHint();
        this.prompt = null;
    }

    _disposeHint() {
        const hint = this._hint;
        this._hint = null;
        if (!hint)
            return;
        try {
            // destroy() unparents as well, so the prompt is left exactly as we
            // found it.
            hint.destroy();
        } catch (e) { /* already gone */ }
    }
}

/* ------------------------------------------------------- polkit <-> dial glue
 *
 * GNOME's app-elevation prompt uses PolkitAgent.Session rather than AuthPrompt,
 * but the PAM conversation is the same. pam_iris announces itself with an
 * "Iris:" info message before opening the camera; a later Password request is
 * the unambiguous hand-off to fallback authentication.
 */
class IrisPolkitBinding {
    constructor(dialog, successHoldMs, onGone = null) {
        this.dialog = dialog;
        this._successHoldMs = successHoldMs;
        this._onGone = onGone;
        this._hint = null;
        this._closedId = 0;
        this._session = null;
        this._sessionIds = [];
        this._sawIris = false;
        this._passwordRequested = false;
        this._originalEmitDone = null;
        this._wrappedEmitDone = null;
        this._holdId = 0;
        this._pendingDone = null;
        this._done = false;
    }

    attach() {
        try {
            if (typeof this.dialog?.contentLayout?.insert_child_at_index !== 'function' ||
                typeof this.dialog?._emitDone !== 'function')
                return false;

            const hint = new IrisHint();
            hint.orientation = Clutter.Orientation.VERTICAL;
            hint.add_style_class_name('iris-polkit-hint');
            this.dialog.contentLayout.insert_child_at_index(hint, 1);
            this._hint = hint;
            hint.setPhase('scanning');

            this._closedId = this.dialog.connect('closed', () => this._onClosed());
            this._wrapDone();
            this._bindSession();

            // The session can emit its first info line before our idle callback
            // attaches. Recover that state from the label the stock dialog set.
            const existing = this.dialog._infoMessageLabel?.text ?? '';
            if (existing.startsWith('Iris:'))
                this._onInfo(existing);
            return true;
        } catch (e) {
            console.debug(`Iris: could not decorate the PolKit prompt: ${e}`);
            this.destroy();
            return false;
        }
    }

    _bindSession() {
        this._disconnectSession();
        let session = null;
        try {
            session = this.dialog?._session ?? null;
        } catch (e) { /* dialog is closing */ }
        if (!session)
            return;

        this._session = session;
        try {
            this._sessionIds.push(session.connect('show-info', (_s, text) =>
                this._onInfo(text)));
            this._sessionIds.push(session.connect('request', (_s, request) =>
                this._onRequest(request)));
            this._sessionIds.push(session.connect('show-error', () =>
                this._hint?.setPhase('failed')));
            this._sessionIds.push(session.connect('completed', (_s, gained) =>
                this._onCompleted(gained)));
        } catch (e) {
            this._disconnectSession();
        }
    }

    _disconnectSession() {
        const session = this._session;
        this._session = null;
        for (const id of this._sessionIds) {
            try {
                session?.disconnect(id);
            } catch (e) { /* session already finalised */ }
        }
        this._sessionIds = [];
    }

    _onInfo(text) {
        if (this._done || typeof text !== 'string' || !text.startsWith('Iris:'))
            return;
        this._sawIris = true;

        if (text.includes('look at the infrared camera')) {
            this._hint?.setPhase('scanning');
            // Replace PAM's plain text banner with the richer visual carrying
            // the same accessible label. Failure details remain untouched.
            try {
                this.dialog._infoMessageLabel.hide();
                this.dialog._nullMessageLabel.show();
            } catch (e) { /* private labels changed; harmless duplication */ }
        } else {
            this._hint?.setPhase('failed');
        }
    }

    _onRequest(_request) {
        if (this._done)
            return;
        this._passwordRequested = true;
        this._hint?.setPhase('waiting');
    }

    _onCompleted(gained) {
        if (this._done || gained)
            return;
        if (this._sawIris)
            this._hint?.setPhase('failed');

        // Stock dialog has already created its retry session by this point.
        this._sawIris = false;
        this._passwordRequested = false;
        this._bindSession();
    }

    _wrapDone() {
        const original = this.dialog._emitDone;
        const binding = this;
        this._originalEmitDone = original;

        this._wrappedEmitDone = function (dismissed) {
            const callThrough = () => {
                binding._pendingDone = null;
                original.call(this, dismissed);
            };

            const faceSuccess = !dismissed && binding._sawIris &&
                !binding._passwordRequested;
            const hold = Math.min(
                Math.max(Number(binding._successHoldMs) || 0, 0),
                SUCCESS_HOLD_MAX_MS);
            if (!faceSuccess || hold <= 0) {
                callThrough();
                return;
            }

            binding._hint?.setPhase('success');
            binding._pendingDone = callThrough;
            binding._holdId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, hold, () => {
                binding._holdId = 0;
                const run = binding._pendingDone;
                binding._pendingDone = null;
                if (run)
                    run();
                return GLib.SOURCE_REMOVE;
            });
        };
        this.dialog._emitDone = this._wrappedEmitDone;
    }

    _releaseDone() {
        if (this._holdId) {
            GLib.Source.remove(this._holdId);
            this._holdId = 0;
        }
        const pending = this._pendingDone;
        this._pendingDone = null;

        try {
            if (this.dialog?._emitDone === this._wrappedEmitDone)
                this.dialog._emitDone = this._originalEmitDone;
        } catch (e) { /* dialog already disposed */ }
        this._originalEmitDone = null;
        this._wrappedEmitDone = null;

        // Never strand an elevation request when the extension is disabled.
        if (pending) {
            try {
                pending();
            } catch (e) { /* stock dialog is already closing */ }
        }
    }

    _onClosed() {
        this._closedId = 0;
        const onGone = this._onGone;
        this.destroy();
        try {
            onGone?.(this);
        } catch (e) { /* owner already disabled */ }
    }

    destroy() {
        if (this._done)
            return;
        this._done = true;
        this._onGone = null;
        this._releaseDone();
        this._disconnectSession();

        if (this._closedId) {
            try {
                this.dialog.disconnect(this._closedId);
            } catch (e) { /* dialog already closed */ }
            this._closedId = 0;
        }

        const hint = this._hint;
        this._hint = null;
        try {
            hint?.destroy();
        } catch (e) { /* child already destroyed with dialog */ }
        this.dialog = null;
    }
}

/* ----------------------------------------------------------- panel indicator */

const IrisIndicator = GObject.registerClass(
class IrisIndicator extends PanelMenu.Button {
    _init(ext) {
        super._init(0.5, 'Iris', false);
        this._ext = ext;
        this._cancellable = new Gio.Cancellable();

        this.add_child(new St.Icon({
            icon_name: 'auth-fingerprint-symbolic',
            style_class: 'system-status-icon',
        }));

        this._status = new PopupMenu.PopupMenuItem('Checking…', {reactive: false});
        this._status.label.add_style_class_name('iris-menu-status');
        this.menu.addMenuItem(this._status);

        this._detail = new PopupMenu.PopupMenuItem('', {reactive: false});
        this._detail.label.add_style_class_name('iris-menu-detail');
        this.menu.addMenuItem(this._detail);

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        const settings = new PopupMenu.PopupMenuItem('Face Authentication Settings…');
        this._settingsId = settings.connect('activate', () => {
            try {
                Gio.Subprocess.new([SETTINGS_BIN], Gio.SubprocessFlags.NONE);
            } catch (e) {
                Main.notify('Iris', 'Could not open the settings app.');
            }
        });
        this.menu.addMenuItem(settings);
        this._settingsItem = settings;

        // Refresh when the menu opens rather than on a timer: a background poll
        // that spawns a process every N seconds is wasteful and shows up in
        // battery usage on a laptop.
        this._openId = this.menu.connect('open-state-changed', (m, open) => {
            if (open)
                this._refresh().catch(() => {});
        });

        this._refresh().catch(() => {});
    }

    async _refresh() {
        const [out, serviceState] = await Promise.all([
            runAsync([IRIS_BIN, 'status', '--json'], this._cancellable),
            runAsync(['/usr/bin/systemctl', 'is-active', 'irisd.service'], this._cancellable),
        ]);
        if (!this._status)
            return; // destroyed while the subprocess was running
        const data = lastJson(out);

        if (!data) {
            this._status.label.text = 'Iris is not responding';
            this._detail.label.text = 'The irisd service may be stopped.';
            return;
        }
        // The daemon socket is deliberately root-only, so an unprivileged ping
        // reports false even while the service is healthy. systemd's read-only
        // state is the authoritative user-visible answer here.
        const running = serviceState?.trim() === 'active' ||
            data.daemon?.running === true || data.running === true;
        const enabled = data.config?.auth?.enabled ?? true;
        let enrolled = data.enrolled ?? data.faces ?? null;
        if (Array.isArray(enrolled))
            enrolled = enrolled.length;

        if (!enabled) {
            this._status.label.text = 'Face authentication is off';
            this._detail.label.text = 'Enable it in Face Authentication Settings.';
        } else if (!running) {
            this._status.label.text = 'Iris service is stopped';
            this._detail.label.text = 'sudo systemctl start irisd';
        } else if (enrolled === 0 || enrolled === false) {
            this._status.label.text = 'No face enrolled';
            this._detail.label.text = 'Open settings to set up face unlock.';
        } else {
            this._status.label.text = 'Face authentication is ready';
            const n = typeof enrolled === 'number' ? enrolled : null;
            this._detail.label.text = n === null
                ? 'Look at the camera when you unlock.'
                : `${n} face${n === 1 ? '' : 's'} enrolled.`;
        }
    }

    destroy() {
        this._cancellable?.cancel();
        this._cancellable = null;
        if (this._openId && this.menu) {
            this.menu.disconnect(this._openId);
            this._openId = null;
        }
        if (this._settingsId && this._settingsItem) {
            this._settingsItem.disconnect(this._settingsId);
            this._settingsId = null;
        }
        this._settingsItem = null;
        this._status = null;
        this._detail = null;
        super.destroy();
    }
});

/* ------------------------------------------------------------------ extension */

/* The dialog is created lazily. On the lock screen ScreenShield.activate() calls
 * _ensureUnlockDialog() *before* it emits active-changed, so by the time we hear
 * anything the dialog is already there — but that is an implementation detail of
 * one release, and the cost of being wrong is a lock screen with no animation
 * and no way to notice. So the signals are backed by a short bounded poll, and
 * the poll stops the moment a dialog is found. */
const DIALOG_RETRY_INTERVAL_MS = 150;
const DIALOG_RETRY_LIMIT = 40;      // ~6 s, then the signals take over again
const POLKIT_RETRY_LIMIT = 20;      // component loading is async at session start

export default class IrisExtension extends Extension {
    enable() {
        this._settings = this.getSettings();
        this._indicator = null;
        this._signals = [];

        this._dialog = null;
        this._dialogDestroyId = 0;
        this._promptBox = null;
        this._promptBoxAddedId = 0;
        this._binding = null;
        this._retryId = 0;
        this._retriesLeft = 0;

        this._polkitAgent = null;
        this._polkitInitiateId = 0;
        this._polkitAttachId = 0;
        this._polkitRetryId = 0;
        this._polkitRetriesLeft = 0;
        this._polkitBinding = null;

        this._syncIndicator();
        this._setupPolkit();

        // Four moments, because between them they cover every route to a dialog:
        // activate() (active-changed), lock()/showDialog() (locked-changed and
        // lock-screen-shown) and waking a screen that was already locked
        // (wake-up-screen). ScreenShield is a plain JS EventEmitter, so these are
        // .connect()/.disconnect() and not GObject handler ids.
        try {
            const shield = Main.screenShield;
            if (shield) {
                for (const name of ['active-changed', 'locked-changed',
                    'lock-screen-shown', 'wake-up-screen']) {
                    const id = shield.connect(name, () => this._onShieldChanged());
                    this._signals.push([shield, id]);
                }
            }
        } catch (e) {
            console.debug(`Iris: could not watch the screen shield: ${e}`);
        }

        // Also a JS EventEmitter. Only used to keep the top-bar indicator in
        // step with the session mode.
        try {
            const id = Main.sessionMode.connect('updated', () => this._syncIndicator());
            this._signals.push([Main.sessionMode, id]);
        } catch (e) { /* nothing to keep in step with, then */ }

        // The greeter's dialog is not built when extensions are enabled: main.js
        // enables them from _initializeUI() and only then defers showDialog() to
        // LayoutManager's 'startup-prepared'. Crucially, ScreenShield.showDialog()
        // emits none of the four signals above — it sets _isLocked directly rather
        // than through _setLocked() — so at GDM _beginTracking()'s bounded poll is
        // otherwise the only route to the prompt, and a boot slower than its ~6 s
        // budget would leave the greeter with no dial and nothing to bring us
        // back. Whichever order the two handlers run in, this restarts that poll
        // at the moment the dialog is created.
        try {
            const id = Main.layoutManager.connect('startup-prepared',
                () => this._onShieldChanged());
            this._signals.push([Main.layoutManager, id]);
        } catch (e) { /* already past startup, or no layout manager */ }

        // Without this the pref is inert until the next login: _syncIndicator()
        // is otherwise only reached from the session-mode signal.
        try {
            const id = this._settings.connect('changed::show-indicator',
                () => this._syncIndicator());
            this._signals.push([this._settings, id]);
        } catch (e) { /* the indicator just stays as it is */ }

        // At GDM this usually finds nothing yet — the dialog arrives a moment
        // later on 'startup-prepared' — so it is the short poll in
        // _beginTracking() that does the attaching, with the signal above as the
        // backstop. In a user session nothing is locked yet and this is a no-op.
        this._onShieldChanged();
    }

    disable() {
        this._stopPolkit();

        // Everything created in enable() is torn down here. A leaked actor inside
        // the unlock dialog is not a tidiness problem, it is a session-wrecking
        // one, so this runs before anything else.
        this._stopTracking();

        for (const [obj, id] of this._signals ?? []) {
            try {
                obj.disconnect(id);
            } catch (e) { /* object already finalised */ }
        }
        this._signals = [];

        this._removeIndicator();
        this._settings = null;
    }

    /* --------------------------------------------------------------- PolKit */

    _setupPolkit() {
        try {
            if (Main.sessionMode.currentMode === 'gdm')
                return;
        } catch (e) { /* normal user session assumed */ }

        if (this._trySetupPolkit())
            return;

        this._polkitRetriesLeft = POLKIT_RETRY_LIMIT;
        this._polkitRetryId = GLib.timeout_add(
            GLib.PRIORITY_DEFAULT,
            DIALOG_RETRY_INTERVAL_MS,
            () => {
                if (this._trySetupPolkit() || --this._polkitRetriesLeft <= 0) {
                    this._polkitRetryId = 0;
                    return GLib.SOURCE_REMOVE;
                }
                return GLib.SOURCE_CONTINUE;
            });
    }

    _trySetupPolkit() {
        let agent = null;
        try {
            agent = Main.componentManager?._allComponents?.polkitAgent ?? null;
        } catch (e) { /* component manager is still starting */ }
        if (!agent)
            return false;
        if (agent === this._polkitAgent)
            return true;

        try {
            this._polkitAgent = agent;
            this._polkitInitiateId = agent.connect('initiate', () =>
                this._queuePolkitAttach());
            this._queuePolkitAttach();
            return true;
        } catch (e) {
            this._polkitAgent = null;
            this._polkitInitiateId = 0;
            return false;
        }
    }

    _queuePolkitAttach() {
        if (this._polkitAttachId)
            return;
        this._polkitAttachId = GLib.idle_add(GLib.PRIORITY_DEFAULT_IDLE, () => {
            this._polkitAttachId = 0;
            let dialog = null;
            try {
                dialog = this._polkitAgent?._currentDialog ?? null;
            } catch (e) { /* request was cancelled immediately */ }
            if (dialog)
                this._attachPolkitDialog(dialog);
            return GLib.SOURCE_REMOVE;
        });
    }

    _attachPolkitDialog(dialog) {
        if (this._polkitBinding?.dialog === dialog)
            return;
        this._polkitBinding?.destroy();
        this._polkitBinding = null;

        let binding = null;
        try {
            binding = new IrisPolkitBinding(dialog, this._successHoldMs(), gone => {
                if (this._polkitBinding === gone)
                    this._polkitBinding = null;
            });
            if (binding.attach())
                this._polkitBinding = binding;
            else
                binding.destroy();
        } catch (e) {
            console.debug(`Iris: not attaching to the PolKit prompt: ${e}`);
            binding?.destroy();
        }
    }

    _stopPolkit() {
        for (const field of ['_polkitAttachId', '_polkitRetryId']) {
            if (this[field]) {
                GLib.Source.remove(this[field]);
                this[field] = 0;
            }
        }
        this._polkitRetriesLeft = 0;

        if (this._polkitAgent && this._polkitInitiateId) {
            try {
                this._polkitAgent.disconnect(this._polkitInitiateId);
            } catch (e) { /* component already disabled */ }
        }
        this._polkitInitiateId = 0;
        this._polkitAgent = null;

        const binding = this._polkitBinding;
        this._polkitBinding = null;
        try {
            binding?.destroy();
        } catch (e) { /* dialog already closed */ }
    }

    /* ------------------------------------------------------------- settings */

    _wantHint() {
        if (!this._settings)
            return false;
        try {
            const key = Main.sessionMode.currentMode === 'gdm'
                ? 'show-on-login-screen'
                : 'show-on-lock-screen';
            return this._settings.get_boolean(key);
        } catch (e) {
            return false;
        }
    }

    /** How long to hold the unlock so the checkmark is seen, from settings. */
    _successHoldMs() {
        try {
            return this._settings?.get_int('success-hold-ms') ?? 0;
        } catch (e) {
            return 0;
        }
    }

    _hintSeconds() {
        try {
            return this._settings?.get_int('hint-timeout') ?? 10;
        } catch (e) {
            return 10;
        }
    }

    /* ------------------------------------------------------------ indicator */

    _syncIndicator() {
        let want = false;
        try {
            want = !!this._settings?.get_boolean('show-indicator') &&
                Main.sessionMode.currentMode !== 'gdm' &&
                !!Main.panel;
        } catch (e) {
            want = false;
        }

        if (want && !this._indicator)
            this._addIndicator();
        else if (!want && this._indicator)
            this._removeIndicator();
    }

    _addIndicator() {
        let indicator = null;
        try {
            indicator = new IrisIndicator(this);
            Main.panel.addToStatusArea('iris', indicator, 0, 'right');
            this._indicator = indicator;
        } catch (e) {
            // Never let a panel failure break the session — but a button that
            // was constructed and then failed to dock still holds a live
            // Gio.Cancellable, two menu signal handlers and possibly an
            // in-flight `iris status` subprocess. Dropping the reference would
            // leave all of that running with nothing able to reach it, so it is
            // destroyed rather than forgotten.
            try {
                indicator?.destroy();
            } catch (e2) { /* already half torn down; nothing more to do */ }
            this._indicator = null;
        }
    }

    _removeIndicator() {
        if (!this._indicator)
            return;
        try {
            this._indicator.destroy();
        } catch (e) { /* already gone */ }
        this._indicator = null;
    }

    /* -------------------------------------------------------- dialog tracking */

    /** Is a prompt something we should currently expect to find? */
    _isPromptExpected() {
        try {
            // The greeter's dialog is up for the whole life of the process.
            if (Main.sessionMode.currentMode === 'gdm')
                return true;
        } catch (e) { /* fall through to the shield */ }

        try {
            return Main.screenShield?.locked ?? false;
        } catch (e) {
            return false;
        }
    }

    _onShieldChanged() {
        if (this._isPromptExpected() && this._wantHint())
            this._beginTracking();
        else
            this._stopTracking();
    }

    /** Idempotent: called again on every shield signal while already attached. */
    _beginTracking() {
        this._cancelRetry();

        if (this._tryTrack())
            return;

        this._retriesLeft = DIALOG_RETRY_LIMIT;
        this._retryId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, DIALOG_RETRY_INTERVAL_MS, () => {
            let found = false;
            try {
                found = this._tryTrack();
            } catch (e) {
                found = false;
            }
            if (found || --this._retriesLeft <= 0) {
                this._retryId = 0;
                return GLib.SOURCE_REMOVE;
            }
            return GLib.SOURCE_CONTINUE;
        });
    }

    _stopTracking() {
        this._cancelRetry();
        this._releaseDialog();
    }

    _cancelRetry() {
        if (this._retryId) {
            GLib.Source.remove(this._retryId);
            this._retryId = 0;
        }
    }

    /** @returns {boolean} true once a dialog has been found and bound. */
    _tryTrack() {
        let dialog = null;
        try {
            // UnlockDialog on the lock screen, LoginDialog at the greeter.
            dialog = Main.screenShield?._dialog ?? null;
        } catch (e) {
            dialog = null;
        }
        if (!dialog)
            return false;

        if (dialog !== this._dialog) {
            this._releaseDialog();
            this._holdDialog(dialog);
        }

        this._syncPrompt();
        return true;
    }

    _holdDialog(dialog) {
        this._dialog = dialog;

        try {
            this._dialogDestroyId = dialog.connect('destroy',
                () => this._onDialogDestroyed());
        } catch (e) {
            this._dialogDestroyId = 0;
        }

        // Lock screen only. UnlockDialog._ensureAuthPrompt() builds the prompt
        // on the swipe up from the clock and _maybeDestroyAuthPrompt() throws it
        // away on the swipe back, over and over, but _promptBox is a plain
        // vertical St.BoxLayout that lives as long as the dialog — so that is
        // what we watch. The prompt is assigned to dialog._authPrompt before
        // add_child() is called, so the identity test below holds.
        //
        // LoginDialog has no _promptBox: its prompt is eager and permanent, and
        // _syncPrompt() picks it up directly.
        try {
            const box = dialog._promptBox ?? null;
            if (box) {
                this._promptBoxAddedId = box.connect('child-added', (_box, child) => {
                    try {
                        if (child === this._dialog?._authPrompt)
                            this._attachTo(child);
                    } catch (e) { /* not the prompt, or the dialog went away */ }
                });
                this._promptBox = box;
            }
        } catch (e) {
            this._promptBox = null;
            this._promptBoxAddedId = 0;
        }
    }

    _syncPrompt() {
        let prompt = null;
        try {
            prompt = this._dialog?._authPrompt ?? null;
        } catch (e) {
            prompt = null;
        }
        // No prompt yet is the normal lock-screen case: the shield is up but the
        // user has not swiped past the clock. 'child-added' will bring us back.
        if (prompt)
            this._attachTo(prompt);
    }

    _attachTo(prompt) {
        if (this._binding?.prompt === prompt)
            return;

        this._detach();

        if (!this._wantHint())
            return;

        let binding = null;
        try {
            // The callback fires when the prompt is destroyed under the binding —
            // on the lock screen, every crossfade back to the clock. Without it
            // we would sit on a spent binding until the next attach.
            binding = new IrisPromptBinding(prompt, this._hintSeconds(), this._successHoldMs(), gone => {
                if (this._binding === gone)
                    this._binding = null;
            });
            if (binding.attach())
                this._binding = binding;
            else
                binding.destroy();
        } catch (e) {
            // If the prompt is not the shape we verified, show nothing. A thrown
            // exception on this path can wedge the unlock and lock the user out
            // of their own machine, which is a far worse outcome than a missing
            // animation.
            console.debug(`Iris: not attaching the dial: ${e}`);
            try {
                binding?.destroy();
            } catch (e2) { /* nothing left to do */ }
            this._binding = null;
        }
    }

    _detach() {
        const binding = this._binding;
        this._binding = null;
        if (!binding)
            return;
        try {
            binding.destroy();
        } catch (e) { /* already torn down */ }
    }

    _onDialogDestroyed() {
        // Fired from C when ScreenShield drops the dialog on unlock. Its
        // children — _promptBox, the prompt, our hint — go with it, so the
        // handler ids on them are already void.
        this._dialogDestroyId = 0;
        this._promptBoxAddedId = 0;
        this._promptBox = null;
        this._detach();
        this._dialog = null;
    }

    _releaseDialog() {
        this._detach();

        if (this._promptBoxAddedId && this._promptBox) {
            try {
                this._promptBox.disconnect(this._promptBoxAddedId);
            } catch (e) { /* already finalised */ }
        }
        this._promptBoxAddedId = 0;
        this._promptBox = null;

        if (this._dialogDestroyId && this._dialog) {
            try {
                this._dialog.disconnect(this._dialogDestroyId);
            } catch (e) { /* already finalised */ }
        }
        this._dialogDestroyId = 0;
        this._dialog = null;
    }
}
