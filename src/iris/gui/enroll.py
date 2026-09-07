"""The enrolment wizard.

Three screens in a crossfading :class:`Gtk.Stack`: name the face, capture it,
see the result.  Everything interesting happens on the middle screen, which is
also where the one piece of awkward hardware truth lives.

The camera handover
-------------------
V4L2 capture devices are exclusive: while the GUI holds ``/dev/video2`` open
for its live preview, ``pkexec iris enroll`` cannot open it, and the enrolment
fails before it starts.  Writing templates into ``/var/lib/iris`` requires
root, so the capture genuinely has to happen in the privileged helper -- which
means the preview and the capture cannot overlap, however much we would like
them to.

So the capture screen runs in three phases:

``preview``
    The GUI owns the camera.  The user sees themselves live in the circular
    mask and lines their face up.  This is the phase the "preview must come
    from the user-side camera" requirement is about, and it is where the user
    actually needs it -- framing is a *before* problem.

``handover``
    The preview worker is stopped, its thread joined and the device released.
    The last frame stays on screen, dimmed, and the shimmer starts, so the
    composition never collapses to an empty circle.  The UI is not blocked for
    any of this: :meth:`PreviewWorker.stop` calls back when the device is free.

``capturing``
    The helper owns the camera.  The ring fills from its progress lines and the
    hints come from the same stream, so the user still gets live feedback --
    just not live video, which nothing on this machine can provide while
    another process is reading the sensor.

The alternative -- previewing from the *colour* camera during capture -- was
rejected: two cameras lighting up for one operation is alarming, and pointing a
preview at a different sensor than the one doing the work is a lie.

The dial
--------
:class:`~.widgets.FaceDial` runs the whole way through, and the phases above map
onto its states from ``docs/ANIMATION.md``:

===================================  ===============================
wizard                               dial
===================================  ===============================
opening the camera                   ``idle`` -- travelling wave
first lit frame, user lining up      ``scanning`` -- faster, breathing
helper reporting samples             ``progress`` -- tweened fill
enrolment finished                   ``success`` -- three beats
anything went wrong                  ``failure`` -- shake, back to idle
===================================  ===============================

Success and failure are *waited on*: the capture screen is held for the length
of the beat before the result screen crossfades in, because a 720 ms animation
that is replaced after 30 ms is worse than no animation at all.  The wait comes
from :meth:`FaceDial.success_duration_ms`, so reduced motion shortens the hold
to its 200 ms fade rather than stalling the wizard for no reason.

Every dial state also changes text on screen -- the hint label is an
``AccessibleRole.STATUS`` live region, and the result title is another -- so
nothing the dial says is said only in motion.

Threading is unchanged and non-negotiable: the camera worker and the helper's
stdout reader are threads, but every callback below is delivered on the main
loop by :func:`GLib.idle_add` inside ``backend``, so widgets are only ever
touched from the main thread.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any, Final

from gi.repository import Adw, GLib, Gtk

from . import backend
from .backend import BackendError, EnrollProgress, EnrollProcess, PreviewWorker
from .widgets import CameraPreview, FaceDial, ScanOverlay, StatusBadge

if TYPE_CHECKING:  # pragma: no cover - import cycle avoidance only
    from .app import IrisWindow

_LOG = logging.getLogger("iris.gui.enroll")

#: Names the template store will accept.  Checked here so the user is told at
#: the point of typing rather than after a polkit prompt and a failed helper.
_NAME_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,63}$")

#: Settling time between releasing the camera and launching the helper.
#: ``VideoCapture.release()`` returns before the kernel has finished tearing
#: down the stream, and an open() that lands inside that window gets EBUSY.
#: One frame period at 15 fps is 67 ms; 200 ms is comfortably clear of it and
#: is under the threshold where a pause reads as a stall.
_HANDOVER_SETTLE_MS: Final[int] = 200

#: Fade applied to the frozen last frame once the helper owns the camera.
_FROZEN_DIM: Final[float] = 0.42

#: Hints to show when the helper does not send its own.  Keyed by the progress
#: value at which each becomes current.  Turning the head between samples is
#: what gives the template angular coverage, so the wizard asks for it even
#: when the helper is silent about pose.
_POSE_HINTS: Final[tuple[tuple[float, str], ...]] = (
    (0.00, "Look straight at the camera"),
    (0.20, "Turn your head slightly to the left"),
    (0.40, "Now slightly to the right"),
    (0.60, "Lift your chin a little"),
    (0.80, "Almost there — look straight ahead"),
)

_PHASE_PREVIEW: Final[str] = "preview"
_PHASE_HANDOVER: Final[str] = "handover"
_PHASE_CAPTURING: Final[str] = "capturing"
_PHASE_DONE: Final[str] = "done"

#: Width of the dial, at a comfortable window size and in a narrow one.  The
#: camera preview and the shimmer are both sized from these through
#: :meth:`FaceDial.inner_diameter`, so the gap between the ticks and the
#: portrait is the spec's ``0.86 x R`` at every size rather than a pair of
#: numbers that happened to look right once.
_DIAL_SIZE: Final[int] = 340
_DIAL_SIZE_COMPACT: Final[int] = 220


class EnrollPage(Adw.NavigationPage):
    """Guides one enrolment from start to finish."""

    __gtype_name__ = "IrisEnrollPage"

    def __init__(self, window: "IrisWindow") -> None:
        super().__init__(title="Set Up Face Unlock")
        self._window = window

        self._worker: PreviewWorker | None = None
        self._process: EnrollProcess | None = None
        #: True only between pressing Continue and tearing the page down.
        #: Every asynchronous callback that would *acquire* something -- the
        #: camera, the privileged helper -- checks it first, because those
        #: callbacks can land after the user has already left: opening
        #: /dev/video2 for a page nobody is looking at would hold the device
        #: against the next attempt.
        self._active = False
        self._phase = _PHASE_PREVIEW
        self._camera: backend.CameraInfo | None = None
        self._hint_text = ""
        self._hint_timeout = 0
        #: Pending crossfade to the result screen, held back while the dial
        #: plays its success or failure beat.  Tracked so :meth:`_teardown` can
        #: cancel it: a wizard the user left must not reappear on screen 3.
        self._result_timeout = 0
        self._enrolled_name = ""

        self._stack = Gtk.Stack(
            transition_type=Gtk.StackTransitionType.CROSSFADE,
            transition_duration=220,  # long enough to read as a fade, not a wipe
            vexpand=True,
        )
        self._stack.add_named(self._build_intro(), "intro")
        self._stack.add_named(self._build_capture(), "capture")
        self._stack.add_named(self._build_result(), "result")

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        toolbar.set_content(self._stack)
        self.set_child(toolbar)

        self._install_escape()

        # Leaving the page by any route -- back button, Escape, the window
        # closing -- must release the camera and abandon the helper. Doing it
        # here rather than in each button handler means there is no exit that
        # can forget to.
        self.connect("hidden", lambda _p: self._teardown())

    def _install_escape(self) -> None:
        """Bind Escape to cancelling the wizard.

        AdwNavigationView pops on Escape by itself, and the ``hidden`` handler
        above would clean up either way -- but relying on that would make the
        behaviour of a documented cancel key an implementation detail of
        another widget. Binding it explicitly, and consuming the event, means
        Escape does exactly one thing here no matter what else is listening.
        """
        controller = Gtk.ShortcutController()
        controller.set_scope(Gtk.ShortcutScope.LOCAL)
        controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        controller.add_shortcut(
            Gtk.Shortcut.new(
                Gtk.ShortcutTrigger.parse_string("Escape"),
                Gtk.CallbackAction.new(lambda *_a: self._on_escape()),
            )
        )
        self.add_controller(controller)

    def _on_escape(self) -> bool:
        """Cancel and leave.  Returns True so nothing else acts on the key."""
        self._cancel()
        return True

    # ------------------------------------------------------------------
    # screen 1: name the face
    # ------------------------------------------------------------------

    def _build_intro(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        box.add_css_class("iris-page")

        heading = Gtk.Label(
            label="Set up Face Unlock",
            halign=Gtk.Align.START,
            wrap=True,
        )
        heading.add_css_class("iris-title-1")

        lede = Gtk.Label(
            label=(
                "Iris takes a series of infrared pictures and turns them into a "
                "mathematical model of your face. The pictures themselves are "
                "never written to disk, and the model stays encrypted on this "
                "computer."
            ),
            halign=Gtk.Align.START,
            xalign=0.0,
            wrap=True,
        )
        lede.add_css_class("iris-lede")

        self._camera_banner = Adw.Banner(
            title="No infrared camera was found.",
            button_label="Open settings",
            revealed=False,
        )
        self._camera_banner.connect("button-clicked", lambda _b: self._window.open_settings())

        group = Adw.PreferencesGroup(
            title="This face",
            description=(
                "You can enrol more than one — for example one with glasses and "
                "one without. Each gets its own name."
            ),
        )

        self._name_row = Adw.EntryRow(title="Name")
        self._name_row.set_text("default")
        self._name_row.connect("changed", lambda _r: self._validate_name())
        self._name_row.connect("entry-activated", lambda _r: self._on_continue())
        group.add(self._name_row)

        self._camera_row = Adw.ActionRow(
            title="Looking for cameras…",
            subtitle="Iris uses the infrared camera so it works in the dark.",
        )
        change = Gtk.Button(label="Change", valign=Gtk.Align.CENTER)
        change.add_css_class("flat")
        change.set_tooltip_text("Choose a different camera in Settings")
        change.connect("clicked", lambda _b: self._window.open_settings())
        self._camera_row.add_suffix(change)
        self._camera_row.set_activatable_widget(change)
        group.add(self._camera_row)

        self._name_error = Gtk.Label(halign=Gtk.Align.START, wrap=True, visible=False)
        self._name_error.add_css_class("iris-caption")
        self._name_error.add_css_class("iris-error-body")

        self._continue_button = Gtk.Button(label="Continue", halign=Gtk.Align.CENTER)
        self._continue_button.add_css_class("iris-cta")
        self._continue_button.add_css_class("suggested-action")
        self._continue_button.connect("clicked", lambda _b: self._on_continue())

        box.append(heading)
        box.append(lede)
        box.append(self._camera_banner)
        box.append(group)
        box.append(self._name_error)
        box.append(self._continue_button)

        return _scrolled(box)

    def _validate_name(self) -> bool:
        """Check the face name and explain, in place, if it will not do."""
        name = self._name_row.get_text().strip()
        if not name:
            problem = "Give this face a name so you can tell it apart later."
        elif not _NAME_RE.match(name):
            problem = (
                "Use letters, numbers, spaces, dots, dashes or underscores, "
                "starting with a letter or number."
            )
        else:
            problem = ""

        self._name_error.set_label(problem)
        self._name_error.set_visible(bool(problem))
        if problem:
            self._name_row.add_css_class("error")
        else:
            self._name_row.remove_css_class("error")
        self._continue_button.set_sensitive(not problem)
        return not problem

    def _on_continue(self) -> None:
        if not self._validate_name():
            return
        self._enrolled_name = self._name_row.get_text().strip()
        self._active = True
        self._stack.set_visible_child_name("capture")
        self._start_preview()

    # ------------------------------------------------------------------
    # screen 2: capture
    # ------------------------------------------------------------------

    def _build_capture(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)
        box.add_css_class("iris-page")
        box.set_valign(Gtk.Align.CENTER)

        # The dial is the base of the overlay, so it defines the stage size;
        # the preview and the shimmer are centred inside it at the diameter the
        # spec reserves for them, which is what leaves the gap between the
        # ticks and the image.
        inner = FaceDial.inner_diameter(_DIAL_SIZE)
        self._dial = FaceDial(natural_size=_DIAL_SIZE)
        self._preview = CameraPreview(natural_size=inner)
        self._preview.set_halign(Gtk.Align.CENTER)
        self._preview.set_valign(Gtk.Align.CENTER)
        self._scan = ScanOverlay()
        self._scan.set_content_width(inner)
        self._scan.set_content_height(inner)
        self._scan.set_halign(Gtk.Align.CENTER)
        self._scan.set_valign(Gtk.Align.CENTER)

        self._stage = Gtk.Overlay(halign=Gtk.Align.CENTER)
        self._stage.add_css_class("iris-stage")
        self._stage.set_child(self._dial)
        self._stage.add_overlay(self._preview)
        self._stage.add_overlay(self._scan)

        self._hint = Gtk.Label(label="Starting the camera…", wrap=True, justify=Gtk.Justification.CENTER)
        self._hint.add_css_class("iris-hint")
        # The hint is the running commentary on a live operation, so screen
        # readers should hear each change without the user going looking.
        self._hint.set_accessible_role(Gtk.AccessibleRole.STATUS)

        self._substep = Gtk.Label(label="", wrap=True, justify=Gtk.Justification.CENTER)
        self._substep.add_css_class("iris-substep")

        self._spinner = Adw.Spinner()
        self._spinner.set_size_request(18, 18)
        self._spinner.set_visible(True)

        busy = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, halign=Gtk.Align.CENTER)
        busy.append(self._spinner)
        busy.append(self._substep)

        self._start_button = Gtk.Button(label="Start")
        self._start_button.add_css_class("iris-cta")
        self._start_button.add_css_class("suggested-action")
        self._start_button.set_sensitive(False)
        self._start_button.connect("clicked", lambda _b: self._begin_capture())

        self._cancel_button = Gtk.Button(label="Cancel")
        self._cancel_button.add_css_class("iris-quiet")
        self._cancel_button.set_tooltip_text("Stop and go back (Escape)")
        self._cancel_button.connect("clicked", lambda _b: self._cancel())

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12,
                          halign=Gtk.Align.CENTER)
        actions.append(self._cancel_button)
        actions.append(self._start_button)

        box.append(self._stage)
        box.append(self._hint)
        box.append(busy)
        box.append(actions)
        return _scrolled(box)

    # -- preview phase --------------------------------------------------

    def _start_preview(self) -> None:
        self._phase = _PHASE_PREVIEW
        # Idle: waiting, no face yet. Nothing has been captured, so the dial
        # goes back to zero as well as back to the travelling wave.
        self._dial.reset()
        self._preview.set_dim(0.0)
        self._set_hint("Starting the camera…")
        self._set_busy(True, "Opening the infrared camera")
        self._start_button.set_visible(True)
        self._start_button.set_sensitive(False)

        # Enumerating cameras imports OpenCV, which takes a moment; do it off
        # the main loop so the crossfade into this screen stays smooth.
        backend.run_async(self._resolve_camera, self._on_camera_resolved)

    def _resolve_camera(self) -> tuple[backend.CameraInfo | None, dict[str, Any]]:
        """Pick the device to preview from: the configured one, else any IR one."""
        cfg = backend.load_config()
        wanted = str(cfg.get("camera", {}).get("device", ""))
        cameras = backend.list_capture_cameras()

        chosen = next((cam for cam in cameras if cam.path == wanted), None)
        if chosen is None:
            # The configured node can vanish across a dock or a kernel update,
            # which renumbers /dev/video*. Falling back beats refusing to run.
            chosen = next((cam for cam in cameras if cam.is_ir), None)
            if chosen is not None:
                _LOG.info("configured camera %s is gone; using %s", wanted, chosen.path)
        return (chosen, cfg)

    def _on_camera_resolved(self, result: Any, error: BackendError | None) -> None:
        if not self._active:
            _LOG.debug("camera resolved after the page was left; not opening it")
            return
        if error is not None:
            self._fail(error)
            return

        camera, cfg = result
        self._camera = camera
        self._update_camera_row(camera)

        if camera is None:
            self._fail(BackendError(
                "Iris could not find a camera to use.",
                "No non-metadata V4L2 capture device is present. Check that the "
                "camera is not disabled in firmware, then try again.",
            ))
            return

        camera_cfg = cfg.get("camera", {})
        self._worker = PreviewWorker(
            device=camera.path,
            width=int(camera_cfg.get("width", 640)),
            height=int(camera_cfg.get("height", 360)),
            # ir_mode drives the GREY format negotiation, so it must follow the
            # device that was actually chosen, not the configured flag -- a
            # fallback to the colour camera would otherwise ask it for GREY.
            ir_mode=camera.is_ir,
            min_brightness=float(camera_cfg.get("min_frame_brightness", 20.0)),
            on_frame=self._on_preview_frame,
            on_error=self._on_preview_error,
            on_stopped=self._on_preview_stopped,
        )
        self._worker.start()

    def _update_camera_row(self, camera: backend.CameraInfo | None) -> None:
        if camera is None:
            self._camera_row.set_title("No camera found")
            self._camera_row.set_subtitle("Connect a camera, or choose one in Settings.")
            self._camera_banner.set_revealed(True)
            return
        self._camera_row.set_title(camera.title)
        self._camera_row.set_subtitle(camera.subtitle)
        self._camera_banner.set_revealed(not camera.is_ir)
        if not camera.is_ir:
            self._camera_banner.set_title(
                "This is a colour camera. Face unlock will not work in the dark."
            )

    def _on_preview_frame(self, frame: backend.PreviewFrame) -> None:
        if self._phase != _PHASE_PREVIEW:
            return  # a frame that was in flight when the phase changed

        texture = frame.to_texture()
        if texture is None:
            # The dark half of the infrared strobe. Keep the last lit frame on
            # screen rather than blanking the circle every 67 ms.
            return
        self._preview.set_texture(texture)

        if not self._start_button.get_sensitive():
            # First *lit* frame: the camera and the emitter are both alive, so
            # unlock the action and say so. Gating on a lit frame matters --
            # enabling Start over a black circle would invite the user to begin
            # before they can see whether they are in shot.
            self._start_button.set_sensitive(True)
            self._start_button.grab_focus()
            # Scanning: there is a live picture of a face to line up, which is
            # what the faster, brighter, breathing ring is for. Called once
            # rather than on every frame -- though FaceDial ignores a repeat of
            # its current state precisely so that a stray call cannot make the
            # ring pulse at 15 fps.
            self._dial.set_scanning()
            self._set_hint("Centre your face in the circle")
            self._set_busy(False, "Ready when you are")

    def _on_preview_error(self, error: BackendError) -> None:
        if self._phase in (_PHASE_CAPTURING, _PHASE_DONE):
            # The helper owns the camera now; a late error from our own worker
            # is expected noise, not something to show the user.
            _LOG.debug("ignoring preview error during %s: %s", self._phase, error.message)
            return
        self._fail(error)

    def _on_preview_stopped(self) -> None:
        """The worker thread has exited and the device is released."""
        if self._phase != _PHASE_HANDOVER or not self._active:
            return
        GLib.timeout_add(_HANDOVER_SETTLE_MS, self._launch_helper)

    # -- handover and capture ------------------------------------------

    def _begin_capture(self) -> None:
        if self._phase != _PHASE_PREVIEW:
            return
        self._phase = _PHASE_HANDOVER
        # Hidden rather than merely disabled: from here until the result there
        # is exactly one thing the user can do, and that is stop.
        self._start_button.set_visible(False)
        self._cancel_button.grab_focus()
        self._scan.start()
        self._preview.set_dim(_FROZEN_DIM)
        # Still scanning: the picture freezes during the handover, but the
        # operation has not paused, and a dial that dropped back to idle here
        # would say it had.
        self._dial.set_scanning()
        self._set_hint("Hold still")
        self._set_busy(True, "Getting the camera ready")

        if self._worker is not None:
            self._worker.stop()  # _on_preview_stopped continues the sequence
        else:  # pragma: no cover - only if the worker never started
            GLib.timeout_add(_HANDOVER_SETTLE_MS, self._launch_helper)

    def _launch_helper(self) -> bool:
        """Start ``pkexec iris enroll``.  Returns ``SOURCE_REMOVE`` for the timer."""
        if self._phase != _PHASE_HANDOVER or not self._active:
            return GLib.SOURCE_REMOVE

        self._phase = _PHASE_CAPTURING
        self._set_busy(True, "Waiting for administrator approval…")
        self._set_hint("Look at the camera")

        self._process = EnrollProcess(
            backend.current_user(),
            self._enrolled_name,
            on_progress=self._on_helper_progress,
            on_stalled=self._on_helper_stalled,
            on_finished=self._on_helper_finished,
        )
        try:
            self._process.start()
        except BackendError as exc:
            self._process = None
            self._fail(exc)
        return GLib.SOURCE_REMOVE

    def _on_helper_progress(self, progress: EnrollProgress) -> None:
        if self._phase != _PHASE_CAPTURING:
            return
        # Progress: samples are accumulating, so the dial fills. The tween is
        # inside the widget, which is why a burst of three samples in one frame
        # still reads as one continuous sweep rather than three jumps.
        self._dial.set_progress(progress.fraction)
        self._set_hint(progress.hint or _pose_hint(progress.fraction))

        if progress.samples is not None and progress.total:
            self._set_busy(True, f"Sample {progress.samples} of {progress.total}")
        else:
            self._set_busy(True, f"{round(progress.fraction * 100)}% complete")

    def _on_helper_stalled(self) -> None:
        """The helper has gone quiet.  Say so honestly instead of pretending."""
        if self._phase != _PHASE_CAPTURING:
            return
        self._set_busy(True, "Still working — this can take a few seconds")

    def _on_helper_finished(
        self, payload: dict[str, Any] | None, error: BackendError | None
    ) -> None:
        self._process = None
        self._scan.stop()

        if error is not None:
            self._fail(error)
            return

        self._phase = _PHASE_DONE
        # Complete the fill outright before the beats begin: the helper can
        # finish from 0.9 if its last samples arrived together, and a success
        # sequence starting from a visibly unfinished ring undercuts itself.
        self._dial.set_progress(1.0, animate=False)
        self._dial.set_success("Face captured")
        self._set_hint("Face captured")
        self._set_busy(False, "Finishing up")

        samples = payload.get("samples") if payload else None
        self._succeed(int(samples) if isinstance(samples, int) else None)

    # ------------------------------------------------------------------
    # screen 3: result
    # ------------------------------------------------------------------

    def _build_result(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)
        box.add_css_class("iris-page")
        box.set_valign(Gtk.Align.CENTER)

        self._badge = StatusBadge(natural_size=88)
        self._badge.set_halign(Gtk.Align.CENTER)

        self._result_title = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        self._result_title.add_css_class("iris-error-title")
        self._result_title.set_accessible_role(Gtk.AccessibleRole.STATUS)

        self._result_body = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER, max_width_chars=52)
        self._result_body.add_css_class("iris-error-body")

        self._detail_label = Gtk.Label(
            wrap=True, xalign=0.0, selectable=True, max_width_chars=64
        )
        self._detail_label.add_css_class("iris-error-detail")
        self._detail_expander = Gtk.Expander(label="Technical details")
        self._detail_expander.set_child(self._detail_label)
        self._detail_expander.set_visible(False)
        self._detail_expander.set_halign(Gtk.Align.CENTER)

        self._primary_button = Gtk.Button()
        self._primary_button.add_css_class("iris-cta")
        self._primary_button.add_css_class("suggested-action")

        self._secondary_button = Gtk.Button()
        self._secondary_button.add_css_class("iris-quiet")

        self._result_actions = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=12, halign=Gtk.Align.CENTER
        )
        self._result_actions.append(self._secondary_button)
        self._result_actions.append(self._primary_button)

        box.append(self._badge)
        box.append(self._result_title)
        box.append(self._result_body)
        box.append(self._detail_expander)
        box.append(self._result_actions)
        return _scrolled(box)

    def _succeed(self, samples: int | None) -> None:
        counted = (
            f" Iris kept {samples} sample{'s' if samples != 1 else ''} of your face."
            if samples else ""
        )
        self._show_result(
            outcome="success",
            title="Face unlock is ready",
            body=(
                "You can now sign in by looking at your laptop."
                f"{counted} Your password still works everywhere it did before."
            ),
            detail="",
            primary=("Done", self._close_page),
            secondary=("Add another face", self._restart),
            # Let the dial finish. The three beats are the moment the whole
            # wizard has been building to, and crossfading over them 30 ms in
            # would be worse than not animating at all.
            after_ms=int(FaceDial.success_duration_ms()),
        )
        self._window.invalidate_faces()

    def _fail(self, error: BackendError) -> None:
        """Show a failure the user can act on.

        The title says what did not happen, the body says what to try, and the
        stderr goes behind an expander. No screen in this wizard tells the user
        they did something wrong -- the failure modes here are cameras, drivers
        and permissions, none of which are their fault.
        """
        self._phase = _PHASE_DONE
        self._scan.stop()
        self._stop_worker()

        cancelled = isinstance(error, backend.AuthorisationCancelled)
        secondary = ("Not now", self._close_page) if cancelled else (
            "Camera settings", self._window.open_settings
        )

        if cancelled:
            # The user closed the password prompt. That is a decision, not a
            # failure, and shaking the ring at someone for making it would be
            # the interface sulking. Straight back to idle, straight to the
            # result screen.
            self._dial.set_idle()
            hold_ms = 0
        else:
            self._dial.set_failure(error.message)
            self._set_hint("That didn't work")
            hold_ms = int(FaceDial.failure_duration_ms())

        self._show_result(
            # A dismissed password prompt is a choice the user made, not a
            # fault: it gets the same words but no red cross.
            outcome="none" if cancelled else "failure",
            title=error.message,
            body=(
                "Nothing was saved, and your existing sign-in options are "
                "unchanged. You can try again whenever you like."
                if error.retryable else
                "Nothing was saved. Your existing sign-in options are unchanged."
            ),
            detail=error.detail,
            primary=("Try again", self._restart) if error.retryable
            else ("Close", self._close_page),
            secondary=secondary,
            after_ms=hold_ms,
        )

    def _show_result(
        self,
        *,
        outcome: str,
        title: str,
        body: str,
        detail: str,
        primary: tuple[str, Any],
        secondary: tuple[str, Any],
        after_ms: int = 0,
    ) -> None:
        """Populate and show the result screen.

        *outcome* is ``"success"``, ``"failure"`` or ``"none"`` -- the last for
        an outcome that is neither, such as the user closing the password
        prompt.

        *after_ms* holds the crossfade back so the dial can finish its beat.
        The screen is *populated* immediately either way: only the transition
        waits, so if the wizard is torn down during the hold nothing has been
        left half-built.
        """
        self._result_title.set_label(title)
        self._result_body.set_label(body)

        self._detail_label.set_label(detail)
        self._detail_expander.set_visible(bool(detail))
        self._detail_expander.set_expanded(False)

        _rebind(self._primary_button, primary[0], primary[1])
        _rebind(self._secondary_button, secondary[0], secondary[1])

        def reveal() -> bool:
            self._result_timeout = 0
            self._stack.set_visible_child_name("result")
            if outcome == "success":
                self._badge.show_success("Face unlock is ready")
            elif outcome == "failure":
                self._badge.show_failure("Enrolment did not finish")
            else:
                self._badge.reset()
            self._primary_button.grab_focus()
            return GLib.SOURCE_REMOVE

        # A second call would otherwise leave two timers racing to reveal two
        # different results.
        if self._result_timeout:
            GLib.source_remove(self._result_timeout)
            self._result_timeout = 0

        if after_ms > 0:
            self._result_timeout = GLib.timeout_add(after_ms, reveal)
        else:
            reveal()

    # ------------------------------------------------------------------
    # navigation and lifecycle
    # ------------------------------------------------------------------

    def reset(self) -> None:
        """Return the wizard to its first screen.  Called before each push."""
        self._teardown()
        self._phase = _PHASE_PREVIEW
        self._dial.reset()
        self._preview.clear()
        self._preview.set_dim(0.0)
        self._badge.reset()
        self._start_button.set_visible(True)
        self._start_button.set_sensitive(False)
        self._stack.set_visible_child_name("intro")
        self._validate_name()
        self._name_row.grab_focus()

        # Name the camera on the first screen, before the user commits to
        # anything: finding out which camera will be used *after* pressing
        # Continue is too late to do anything about it.
        backend.run_async(self._resolve_camera, self._on_camera_probed)

    def _on_camera_probed(self, result: Any, error: BackendError | None) -> None:
        """Fill in the intro screen's camera row.  Never starts the camera."""
        if error is not None:
            self._camera_row.set_title("Could not check the cameras")
            self._camera_row.set_subtitle(error.message)
            return
        camera, _cfg = result
        self._camera = camera
        self._update_camera_row(camera)

    def _restart(self) -> None:
        self.reset()

    def _close_page(self) -> None:
        self._teardown()
        self._window.pop_page()

    def _cancel(self) -> None:
        """Abandon the enrolment and leave.  Bound to Cancel and to Escape."""
        self._close_page()

    def _teardown(self) -> None:
        """Release every resource this page owns.  Safe to call repeatedly."""
        self._active = False
        self._stop_worker()
        if self._process is not None:
            self._process.cancel()
            self._process = None
        if self._hint_timeout:
            GLib.source_remove(self._hint_timeout)
            self._hint_timeout = 0
        if self._result_timeout:
            # A result that was waiting on the dial must not arrive after the
            # user has walked away from the wizard.
            GLib.source_remove(self._result_timeout)
            self._result_timeout = 0
        self._scan.stop()

    def _stop_worker(self) -> None:
        if self._worker is not None:
            self._worker.stop()
            self._worker = None

    def set_compact(self, compact: bool) -> None:
        """Shrink the portrait stage on a narrow window.

        The preview follows the dial rather than being sized independently, so
        the ``0.86 x R`` gap survives the breakpoint.
        """
        dial_size = _DIAL_SIZE_COMPACT if compact else _DIAL_SIZE
        inner = FaceDial.inner_diameter(dial_size)
        self._dial.set_content_width(dial_size)
        self._dial.set_content_height(dial_size)
        self._preview.set_natural_size(inner)
        self._scan.set_content_width(inner)
        self._scan.set_content_height(inner)

    # ------------------------------------------------------------------
    # small helpers
    # ------------------------------------------------------------------

    def _set_hint(self, text: str) -> None:
        """Swap the pose hint with a short dip in opacity.

        A hard text swap on a 20 px label is a visual snap that pulls the eye
        away from the camera at exactly the wrong moment; fading through makes
        the same change register as calm.
        """
        if text == self._hint_text:
            return
        self._hint_text = text

        if self._hint_timeout:
            GLib.source_remove(self._hint_timeout)
        self._hint.add_css_class("iris-settling")

        def commit() -> bool:
            self._hint.set_label(text)
            self._hint.remove_css_class("iris-settling")
            self._hint_timeout = 0
            return GLib.SOURCE_REMOVE

        # Matches the 180 ms opacity transition in style.css: swap the text at
        # the bottom of the dip, where the change is invisible.
        self._hint_timeout = GLib.timeout_add(180, commit)

    def _set_busy(self, busy: bool, text: str) -> None:
        self._spinner.set_visible(busy)
        self._substep.set_label(text)


def _pose_hint(fraction: float) -> str:
    """The hint for this point in the capture, when the helper sends none."""
    current = _POSE_HINTS[0][1]
    for threshold, text in _POSE_HINTS:
        if fraction >= threshold:
            current = text
    return current


def _rebind(button: Gtk.Button, label: str, handler: Any) -> None:
    """Point a reused button at a new action.

    The result screen has two buttons whose meaning changes with the outcome.
    Rebinding rather than rebuilding keeps the focus chain and the crossfade
    stable, but every old handler must go or the button would fire both.
    """
    button.set_label(label)
    previous = getattr(button, "_iris_handler", 0)
    if previous:
        button.disconnect(previous)
    button._iris_handler = button.connect("clicked", lambda _b: handler())  # type: ignore[attr-defined]


def _scrolled(child: Gtk.Widget) -> Gtk.ScrolledWindow:
    """Wrap a screen so it stays usable in a short window.

    Horizontal scrolling is disabled outright: content that runs off the side
    is a layout bug, and letting the user scroll to it hides the bug instead of
    showing it. Vertical scrolling is the honest answer to a window that is
    simply not tall enough.
    """
    clamp = Adw.Clamp(maximum_size=560, tightening_threshold=440, child=child)
    scroller = Gtk.ScrolledWindow(
        hscrollbar_policy=Gtk.PolicyType.NEVER,
        vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
        vexpand=True,
    )
    scroller.set_child(clamp)
    return scroller
