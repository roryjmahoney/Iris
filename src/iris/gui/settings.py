"""Settings, the camera test, and management of enrolled faces.

Why settings are staged rather than instant-apply
-------------------------------------------------
GNOME's convention is that a settings control takes effect the moment you touch
it.  Iris cannot follow it: ``/etc/iris/config.toml`` is ``root:root 0644``, so
every write goes through ``pkexec`` and raises a polkit prompt.  Instant-apply
would mean a password prompt per switch flipped and per pixel the slider moved,
which is both unusable and a good way to train people to type their password
without reading the dialog.

So changes are staged in memory, a banner says plainly that they have not taken
effect, and "Apply" writes the whole configuration in one privileged call --
one prompt for the whole visit.  :func:`iris.config.save_config` merges over
the defaults and writes atomically, so sending the entire document is both
correct and cheaper than a diff.

The faces list is built as a reusable :class:`FacesGroup` so the same list,
with the same behaviour, appears both inside Settings and on its own page.
"""

from __future__ import annotations

import copy
import logging
from typing import TYPE_CHECKING, Any, Callable, Final

from gi.repository import Adw, GLib, Gtk

from . import backend
from .backend import BackendError, PreviewFrame, PreviewWorker
from .widgets import CameraPreview

if TYPE_CHECKING:  # pragma: no cover - import cycle avoidance only
    from .app import IrisWindow

_LOG = logging.getLogger("iris.gui.settings")

#: Slider bounds.  Below 0.30 the system starts accepting strangers; above
#: 0.70 it is stricter than the worst genuine sample measured on this hardware
#: (0.621, see docs/CALIBRATION.md) and would reject the enrolled user.
_THRESHOLD_MIN: Final[float] = 0.30
_THRESHOLD_MAX: Final[float] = 0.70
_THRESHOLD_STEP: Final[float] = 0.005

#: Named points on the slider, from the calibration study.
_THRESHOLD_MARKS: Final[tuple[tuple[float, str], ...]] = (
    (0.363, "Standard"),
    (0.500, "Recommended"),
    (0.600, "Strict"),
)

#: iris.config clamps auth.timeout to [0.5, 60]; the useful range is narrower.
#: Under a second there is no time for the emitter to strobe twice, and past
#: half a minute a failed login feels broken rather than slow.
_TIMEOUT_MIN: Final[float] = 2.0
_TIMEOUT_MAX: Final[float] = 30.0

#: How long the camera test waits before concluding that nothing is arriving.
_TEST_PATIENCE_MS: Final[int] = 5000


# --------------------------------------------------------------------------
# Settings page
# --------------------------------------------------------------------------

class SettingsPage(Adw.NavigationPage):
    """Camera choice, recognition strictness, timeouts and face data."""

    __gtype_name__ = "IrisSettingsPage"

    def __init__(self, window: "IrisWindow") -> None:
        super().__init__(title="Settings")
        self._window = window

        self._saved: dict[str, Any] = backend.load_config()
        self._pending: dict[str, Any] = copy.deepcopy(self._saved)
        self._cameras: list[backend.CameraInfo] = []
        #: Set while code is writing to the widgets, so their change handlers
        #: do not mistake a programmatic update for something the user did.
        self._loading = True

        self._banner = Adw.Banner(
            title="These changes need administrator approval before they take effect.",
            revealed=False,
        )

        self._apply_button = Gtk.Button(label="Apply")
        self._apply_button.add_css_class("suggested-action")
        self._apply_button.set_sensitive(False)
        self._apply_button.set_tooltip_text("Save these settings (asks for your password)")
        self._apply_button.connect("clicked", lambda _b: self._apply())

        self._discard_button = Gtk.Button(label="Discard")
        self._discard_button.add_css_class("flat")
        self._discard_button.set_visible(False)
        self._discard_button.connect("clicked", lambda _b: self._discard())

        header = Adw.HeaderBar()
        header.pack_start(self._discard_button)
        header.pack_end(self._apply_button)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        content.append(self._banner)
        content.append(self._build_body())

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(content)
        self.set_child(toolbar)

        self.connect("showing", lambda _p: self._on_showing())
        self.connect("hidden", lambda _p: self._on_hidden())

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------

    def _build_body(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=28)
        page.add_css_class("iris-page")

        page.append(self._build_general_group())
        page.append(self._build_camera_group())
        page.append(self._build_recognition_group())

        self._faces_group = FacesGroup(self._window)
        page.append(self._faces_group)

        clamp = Adw.Clamp(maximum_size=680, tightening_threshold=560, child=page)
        scroller = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            vexpand=True,
        )
        scroller.set_child(clamp)
        return scroller

    def _build_general_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title="Face unlock")

        self._enabled_row = Adw.SwitchRow(
            title="Sign in with your face",
            subtitle=(
                "When this is off Iris never opens the camera, and you sign in "
                "with your password as usual."
            ),
        )
        self._enabled_row.connect("notify::active", lambda *_a: self._on_enabled_changed())
        group.add(self._enabled_row)

        self._timeout_row = Adw.SpinRow.new_with_range(_TIMEOUT_MIN, _TIMEOUT_MAX, 1.0)
        # The unit lives in the title rather than in a formatted value: an
        # AdwSpinRow "output" handler has to write through GtkEditable, which
        # re-enters the row's own input parsing and leaves the field blank.
        self._timeout_row.set_title("Give up after (seconds)")
        self._timeout_row.set_subtitle(
            "How long the camera looks for you before the password box takes over."
        )
        self._timeout_row.connect("notify::value", lambda *_a: self._on_timeout_changed())
        group.add(self._timeout_row)

        return group

    def _build_camera_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title="Camera",
            description=(
                "Infrared cameras are listed first. They work in the dark and are "
                "much harder to fool with a photograph, so Iris prefers them."
            ),
        )

        self._camera_row = Adw.ComboRow(title="Camera", subtitle="Looking for cameras…")
        self._camera_row.set_model(Gtk.StringList.new(["Looking for cameras…"]))
        self._camera_row.set_sensitive(False)
        self._camera_row.connect("notify::selected", lambda *_a: self._on_camera_changed())
        group.add(self._camera_row)

        self._test_row = Adw.ActionRow(
            title="Test this camera",
            subtitle="See a live picture and check that the infrared light is working.",
        )
        test_button = Gtk.Button(label="Test camera", valign=Gtk.Align.CENTER)
        test_button.connect("clicked", lambda _b: self._open_camera_test())
        self._test_row.add_suffix(test_button)
        self._test_row.set_activatable_widget(test_button)
        group.add(self._test_row)

        return group

    def _build_recognition_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title="How certain Iris has to be",
            description=(
                "Iris compares what the camera sees with the model it saved when "
                "you enrolled, and scores the similarity."
            ),
        )

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.add_css_class("iris-well")

        self._threshold_scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, _THRESHOLD_MIN, _THRESHOLD_MAX, _THRESHOLD_STEP
        )
        self._threshold_scale.set_draw_value(False)
        self._threshold_scale.set_hexpand(True)
        for value, label in _THRESHOLD_MARKS:
            self._threshold_scale.add_mark(value, Gtk.PositionType.BOTTOM, label)
        # Arrow keys move by one step; Page Up/Down by ten. Without this a
        # keyboard user needs eighty presses to cross the useful range.
        self._threshold_scale.get_adjustment().set_page_increment(_THRESHOLD_STEP * 10)
        self._threshold_scale.update_property(
            [Gtk.AccessibleProperty.LABEL],
            ["Required similarity score"],
        )
        self._threshold_scale.connect("value-changed", lambda _s: self._on_threshold_changed())

        self._threshold_value = Gtk.Label(halign=Gtk.Align.END)
        self._threshold_value.add_css_class("iris-numeric")
        self._threshold_value.add_css_class("iris-caption")

        self._threshold_explainer = Gtk.Label(
            wrap=True, xalign=0.0, halign=Gtk.Align.START, justify=Gtk.Justification.LEFT
        )
        self._threshold_explainer.add_css_class("iris-explainer")
        self._threshold_explainer.set_accessible_role(Gtk.AccessibleRole.STATUS)

        box.append(self._threshold_scale)
        box.append(self._threshold_value)
        box.append(self._threshold_explainer)
        group.add(box)

        return group

    # ------------------------------------------------------------------
    # loading and staging
    # ------------------------------------------------------------------

    def _on_showing(self) -> None:
        """Re-read from disk each time the page appears.

        The daemon, the CLI or an administrator with an editor can all have
        changed the file since the window opened; showing stale values and then
        writing them back would quietly undo someone else's work.
        """
        self._saved = backend.load_config()
        self._pending = copy.deepcopy(self._saved)
        self._load_widgets()
        self._faces_group.refresh()
        backend.run_async(backend.list_capture_cameras, self._on_cameras_loaded)

    def _on_hidden(self) -> None:
        if self._is_dirty():
            # Nothing is lost silently: the values are still on the page when
            # the user comes back, and the banner still says they are unsaved.
            _LOG.info("leaving settings with unapplied changes")

    def _load_widgets(self) -> None:
        self._loading = True
        try:
            auth = self._pending.setdefault("auth", {})
            recognition = self._pending.setdefault("recognition", {})

            self._enabled_row.set_active(bool(auth.get("enabled", True)))
            self._timeout_row.set_value(
                min(_TIMEOUT_MAX, max(_TIMEOUT_MIN, float(auth.get("timeout", 8.0))))
            )
            # The fallback is the shipped default (0.363), not the 0.500 this
            # panel *recommends*. Showing the recommendation as if it were the
            # current value would mean the slider disagrees with the daemon, and
            # pressing Apply would silently tighten the threshold the user never
            # touched.
            from iris.config import DEFAULTS

            self._threshold_scale.set_value(
                min(_THRESHOLD_MAX, max(_THRESHOLD_MIN, float(
                    recognition.get("threshold", DEFAULTS["recognition"]["threshold"])
                )))
            )
            self._update_threshold_text(self._threshold_scale.get_value())
        finally:
            self._loading = False
        self._refresh_dirty()

    def _on_cameras_loaded(self, cameras: Any, error: BackendError | None) -> None:
        if error is not None:
            self._camera_row.set_subtitle(error.message)
            self._camera_row.set_sensitive(False)
            self._test_row.set_sensitive(False)
            return

        self._cameras = list(cameras)
        if not self._cameras:
            self._camera_row.set_model(Gtk.StringList.new(["No camera found"]))
            self._camera_row.set_subtitle(
                "No usable camera is connected. Metadata-only devices are hidden "
                "because they carry no picture."
            )
            self._camera_row.set_sensitive(False)
            self._test_row.set_sensitive(False)
            return

        self._loading = True
        try:
            labels = [
                f"{cam.title} — {'Infrared' if cam.is_ir else 'Colour'}"
                for cam in self._cameras
            ]
            self._camera_row.set_model(Gtk.StringList.new(labels))
            self._camera_row.set_sensitive(True)
            self._test_row.set_sensitive(True)
            self._camera_row.set_selected(self._preferred_index())
        finally:
            self._loading = False
        self._sync_camera_subtitle()

        # A configured device that is no longer present means the combo now
        # shows a *different* camera from the one in the file. Staging that as
        # a pending change makes the discrepancy visible and fixable in one
        # click, instead of silently leaving a dead device path on disk.
        configured = str(self._pending.get("camera", {}).get("device", ""))
        if configured and all(camera.path != configured for camera in self._cameras):
            self._on_camera_changed()
            self._window.toast("The saved camera is missing — Iris picked another")

    def _preferred_index(self) -> int:
        """Index of the configured camera, else the first infrared one, else 0."""
        wanted = str(self._pending.get("camera", {}).get("device", ""))
        for index, camera in enumerate(self._cameras):
            if camera.path == wanted:
                return index
        for index, camera in enumerate(self._cameras):
            if camera.is_ir:
                return index
        return 0

    def _selected_camera(self) -> backend.CameraInfo | None:
        index = self._camera_row.get_selected()
        if 0 <= index < len(self._cameras):
            return self._cameras[index]
        return None

    def _sync_camera_subtitle(self) -> None:
        camera = self._selected_camera()
        self._camera_row.set_subtitle(camera.subtitle if camera else "No camera selected")

    # -- change handlers ------------------------------------------------

    def _on_enabled_changed(self) -> None:
        if self._loading:
            return
        self._pending.setdefault("auth", {})["enabled"] = self._enabled_row.get_active()
        self._refresh_dirty()

    def _on_timeout_changed(self) -> None:
        if self._loading:
            return
        self._pending.setdefault("auth", {})["timeout"] = float(self._timeout_row.get_value())
        self._refresh_dirty()

    def _on_threshold_changed(self) -> None:
        value = self._threshold_scale.get_value()
        self._update_threshold_text(value)
        if self._loading:
            return
        self._pending.setdefault("recognition", {})["threshold"] = round(value, 4)
        self._refresh_dirty()

    def _on_camera_changed(self) -> None:
        self._sync_camera_subtitle()
        if self._loading:
            return
        camera = self._selected_camera()
        if camera is None:
            return
        camera_cfg = self._pending.setdefault("camera", {})
        camera_cfg["device"] = camera.path
        # ir_mode is not cosmetic: it selects the GREY pixel format and turns on
        # the dark-frame filter that the strobing emitter makes necessary.
        # Asking a colour camera for GREY, or filtering its (uniformly bright)
        # frames for illumination, would both go wrong -- so it follows the
        # device rather than being a separate switch the user has to get right.
        camera_cfg["ir_mode"] = camera.is_ir
        self._refresh_dirty()

    def _update_threshold_text(self, value: float) -> None:
        self._threshold_value.set_label(f"Similarity score {value:.3f}")

        for css_class in ("iris-strict", "iris-loose"):
            self._threshold_explainer.remove_css_class(css_class)

        if value < 0.42:
            self._threshold_explainer.add_css_class("iris-loose")
            text = (
                "Lenient. You will almost never be asked twice, but a sibling or "
                "a good photograph has a better chance of getting in."
            )
        elif value <= 0.56:
            text = (
                "Balanced. On this camera every genuine match measured well above "
                "this line, so you should get in first time while look-alikes do "
                "not."
            )
        else:
            self._threshold_explainer.add_css_class("iris-strict")
            text = (
                "Strict. The safest setting, at the cost of the occasional retry "
                "at an awkward angle or in unusual lighting."
            )
        self._threshold_explainer.set_label(text)

    # -- apply / discard ------------------------------------------------

    def _is_dirty(self) -> bool:
        return self._pending != self._saved

    def _refresh_dirty(self) -> None:
        dirty = self._is_dirty()
        self._banner.set_revealed(dirty)
        self._apply_button.set_sensitive(dirty)
        self._discard_button.set_visible(dirty)

    def _discard(self) -> None:
        self._pending = copy.deepcopy(self._saved)
        self._load_widgets()
        self._loading = True
        try:
            self._camera_row.set_selected(self._preferred_index())
        finally:
            self._loading = False
        self._sync_camera_subtitle()
        self._window.toast("Changes discarded")

    def _apply(self) -> None:
        self._set_applying(True)
        pending = copy.deepcopy(self._pending)
        backend.run_async(
            lambda: backend.save_config(pending),
            lambda _result, error: self._on_applied(pending, error),
        )

    def _set_applying(self, applying: bool) -> None:
        self._apply_button.set_sensitive(not applying)
        self._apply_button.set_label("Saving…" if applying else "Apply")
        self._discard_button.set_sensitive(not applying)

    def _on_applied(self, pending: dict[str, Any], error: BackendError | None) -> None:
        self._set_applying(False)
        if error is not None:
            self._refresh_dirty()
            if isinstance(error, backend.AuthorisationCancelled):
                self._window.toast("Settings were not saved")
            else:
                _show_error(self._window, "Those settings could not be saved", error,
                            retry=self._apply if error.retryable else None)
            return

        self._saved = pending
        self._pending = copy.deepcopy(pending)
        self._refresh_dirty()
        self._window.toast("Settings saved")
        self._window.reload_config()

    # -- camera test ----------------------------------------------------

    def _open_camera_test(self) -> None:
        camera = self._selected_camera()
        if camera is None:
            self._window.toast("Choose a camera first")
            return
        CameraTestDialog(camera, self._pending).present(self._window)

    def set_compact(self, compact: bool) -> None:
        """Marks in a slider need room; drop the labels on a narrow window."""
        self._threshold_scale.clear_marks()
        if not compact:
            for value, label in _THRESHOLD_MARKS:
                self._threshold_scale.add_mark(value, Gtk.PositionType.BOTTOM, label)
        else:
            for value, _label in _THRESHOLD_MARKS:
                self._threshold_scale.add_mark(value, Gtk.PositionType.BOTTOM, None)


# --------------------------------------------------------------------------
# Enrolled faces
# --------------------------------------------------------------------------

class FacesGroup(Adw.PreferencesGroup):
    """The list of enrolled faces, with per-face and bulk deletion.

    Reading the list needs root, so the group has a *locked* state as well as
    the usual loading/empty/populated ones.  Opening Settings must not fire a
    polkit prompt on its own -- an unexpected password dialog is exactly the
    thing people learn to click through -- so the privileged read only happens
    when the user asks for it by pressing Unlock.
    """

    __gtype_name__ = "IrisFacesGroup"

    def __init__(self, window: "IrisWindow") -> None:
        super().__init__(
            title="Your face data",
            description=(
                "Iris stores a mathematical model of your face, encrypted, on this "
                "computer. It never stores pictures and nothing is sent anywhere."
            ),
        )
        self._window = window
        self._rows: list[Gtk.Widget] = []
        self._faces: list[backend.Face] = []
        self._busy = False

        # The list is fetched when the containing page is shown, not here:
        # constructing the group must not spawn a subprocess for a page the
        # user may never look at.
        self._show_loading()

    # -- state ----------------------------------------------------------

    def refresh(self, *, allow_prompt: bool = False) -> None:
        """Reload the list.  Set *allow_prompt* only from an explicit unlock."""
        if self._busy:
            return
        self._busy = True
        self._show_loading()
        user = backend.current_user()
        backend.run_async(
            lambda: backend.fetch_faces(user, allow_prompt=allow_prompt),
            self._on_loaded,
        )

    def _on_loaded(self, faces: Any, error: BackendError | None) -> None:
        self._busy = False
        if error is not None:
            if isinstance(error, backend.AuthorisationCancelled):
                self._show_locked()
                return
            self._show_locked(error)
            return
        self._faces = list(faces)
        self._show_faces()

    def _clear_rows(self) -> None:
        for row in self._rows:
            self.remove(row)
        self._rows.clear()

    def _add(self, row: Gtk.Widget) -> None:
        self.add(row)
        self._rows.append(row)

    def _show_loading(self) -> None:
        self._clear_rows()
        row = Adw.ActionRow(title="Checking your face data…")
        spinner = Adw.Spinner()
        spinner.set_size_request(18, 18)
        row.add_suffix(spinner)
        self._add(row)

    def _show_locked(self, error: BackendError | None = None) -> None:
        self._clear_rows()

        # A non-retryable error means authorising would not help -- the helper
        # is missing, or this account may not manage face data. Offering an
        # Unlock button there just sends the user through a password prompt to
        # reach the same dead end, so say what is actually wrong instead.
        if error is not None and not error.retryable:
            row = Adw.ActionRow(title=error.message, subtitle=error.detail or "")
            row.add_prefix(Gtk.Image(icon_name="dialog-information-symbolic"))
            row.set_subtitle_lines(3)
            self._add(row)
            return

        row = Adw.ActionRow(
            title="Face data is protected",
            subtitle=(
                error.message if error is not None else
                "Unlock to see which faces are enrolled and to remove them."
            ),
        )
        unlock = Gtk.Button(label="Unlock", valign=Gtk.Align.CENTER)
        unlock.add_css_class("suggested-action")
        unlock.set_tooltip_text("Asks for your password, then shows your enrolled faces")
        unlock.connect("clicked", lambda _b: self.refresh(allow_prompt=True))
        row.add_suffix(unlock)
        row.set_activatable_widget(unlock)
        self._add(row)

    def _show_faces(self) -> None:
        self._clear_rows()

        if not self._faces:
            row = Adw.ActionRow(
                title="No face is set up yet",
                subtitle="Enrol one and you can sign in by looking at your laptop.",
            )
            setup = Gtk.Button(label="Set up", valign=Gtk.Align.CENTER)
            setup.add_css_class("suggested-action")
            setup.connect("clicked", lambda _b: self._window.open_enroll())
            row.add_suffix(setup)
            row.set_activatable_widget(setup)
            self._add(row)
            return

        for face in self._faces:
            row = Adw.ActionRow(title=face.title, subtitle=face.subtitle)
            delete = Gtk.Button(
                icon_name="user-trash-symbolic",
                valign=Gtk.Align.CENTER,
                tooltip_text=f"Remove “{face.title}”",
            )
            delete.add_css_class("flat")
            delete.update_property(
                [Gtk.AccessibleProperty.LABEL], [f"Remove the face named {face.title}"]
            )
            delete.connect("clicked", lambda _b, f=face: self._confirm_remove(f))
            row.add_suffix(delete)
            self._add(row)

        remove_all = Adw.ButtonRow(title="Remove all face data")
        remove_all.add_css_class("destructive-action")
        remove_all.connect("activated", lambda _r: self._confirm_clear())
        self._add(remove_all)

    # -- destructive actions --------------------------------------------

    def _confirm_remove(self, face: backend.Face) -> None:
        dialog = Adw.AlertDialog(
            heading=f"Remove “{face.title}”?",
            body=(
                "You will not be able to sign in with this face until you enrol "
                "it again. Your password is unaffected."
            ),
        )
        dialog.add_response("cancel", "Keep it")
        dialog.add_response("remove", "Remove")
        dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._on_remove_response, face)
        dialog.present(self._window)

    def _on_remove_response(self, _dialog: Adw.AlertDialog, response: str,
                            face: backend.Face) -> None:
        if response != "remove":
            return
        user = backend.current_user()
        self._show_loading()
        self._busy = True
        remaining = [f for f in self._faces if f.name != face.name]
        backend.run_async(
            lambda: backend.remove_face(user, face.name),
            lambda _r, error: self._on_removed(error, remaining, f"Removed “{face.title}”"),
        )

    def _confirm_clear(self) -> None:
        dialog = Adw.AlertDialog(
            heading="Remove all face data?",
            body=(
                "Every face model Iris has saved for you is deleted immediately "
                "and cannot be recovered. You will sign in with your password "
                "until you set up face unlock again."
            ),
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("clear", "Remove everything")
        dialog.set_response_appearance("clear", Adw.ResponseAppearance.DESTRUCTIVE)
        # The safe answer is the default and the Escape answer, so a stray
        # Return or Escape can never destroy the user's enrolment.
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._on_clear_response)
        dialog.present(self._window)

    def _on_clear_response(self, _dialog: Adw.AlertDialog, response: str) -> None:
        if response != "clear":
            return
        user = backend.current_user()
        self._show_loading()
        self._busy = True
        backend.run_async(
            lambda: backend.clear_faces(user),
            lambda _r, error: self._on_removed(error, [], "All face data removed"),
        )

    def _on_removed(
        self,
        error: BackendError | None,
        remaining: list[backend.Face],
        message: str,
    ) -> None:
        self._busy = False
        if error is not None:
            if isinstance(error, backend.AuthorisationCancelled):
                self._window.toast("Nothing was removed")
            else:
                _show_error(self._window, "That could not be removed", error)
            # What is on screen is now unknown rather than merely stale, so
            # re-read it -- without prompting, since the user has just been
            # through one authorisation that did not work out.
            self.refresh(allow_prompt=False)
            return

        # The helper reported success, so the resulting list is known exactly.
        # Re-reading it would mean a second privileged call, and therefore a
        # second password prompt on a system whose polkit rule does not cache
        # the first one -- two prompts to delete one face.
        self._faces = remaining
        self._show_faces()
        self._window.toast(message)
        self._window.invalidate_faces()


class FacesPage(Adw.NavigationPage):
    """A page whose whole content is the enrolled-faces list."""

    __gtype_name__ = "IrisFacesPage"

    def __init__(self, window: "IrisWindow") -> None:
        super().__init__(title="Your Faces")
        self._group = FacesGroup(window)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        body.add_css_class("iris-page")
        body.append(self._group)

        scroller = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            vexpand=True,
        )
        scroller.set_child(Adw.Clamp(maximum_size=680, tightening_threshold=560, child=body))

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        toolbar.set_content(scroller)
        self.set_child(toolbar)

        self.connect("showing", lambda _p: self._group.refresh())


# --------------------------------------------------------------------------
# Camera test
# --------------------------------------------------------------------------

class CameraTestDialog(Adw.Dialog):
    """Live preview plus a verdict on whether this camera can actually be used.

    "Test camera" has to answer more than "does a picture appear".  On this
    hardware the interesting failure is an infrared node that streams perfectly
    while its emitter never fires: every frame arrives, every frame is nearly
    black, and authentication fails with a mystifying "no face".  So the test
    watches the brightness of both halves of the strobe and says outright
    whether the illuminator is working.
    """

    __gtype_name__ = "IrisCameraTestDialog"

    def __init__(self, camera: backend.CameraInfo, config: dict[str, Any]) -> None:
        super().__init__(title="Camera test", content_width=460, content_height=580)
        self._camera = camera
        self._config = config

        self._frames = 0
        self._lit = 0
        self._dark = 0
        self._faces = 0
        self._brightest = 0.0
        self._darkest = 255.0
        self._min_brightness = float(
            config.get("camera", {}).get("min_frame_brightness", 20.0)
        )
        self._patience_source = 0

        self._preview = CameraPreview(natural_size=260)
        self._preview.set_halign(Gtk.Align.CENTER)

        self._verdict = Gtk.Label(
            label="Starting the camera…",
            wrap=True,
            justify=Gtk.Justification.CENTER,
            max_width_chars=40,
        )
        self._verdict.add_css_class("iris-body")
        self._verdict.set_accessible_role(Gtk.AccessibleRole.STATUS)

        self._readout = Gtk.Label(label="", wrap=True, justify=Gtk.Justification.CENTER)
        self._readout.add_css_class("iris-readout")

        close = Gtk.Button(label="Close", halign=Gtk.Align.CENTER)
        close.add_css_class("iris-quiet")
        close.connect("clicked", lambda _b: self.close())

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        body.add_css_class("iris-page")
        body.append(self._preview)
        body.append(self._verdict)
        body.append(self._readout)
        body.append(close)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        toolbar.set_content(body)
        self.set_child(toolbar)

        self._worker = PreviewWorker(
            device=camera.path,
            width=int(config.get("camera", {}).get("width", 640)),
            height=int(config.get("camera", {}).get("height", 360)),
            ir_mode=camera.is_ir,
            min_brightness=self._min_brightness,
            # Detection is what turns "frames are arriving" into "it can see
            # you", which is the question the user is actually asking.
            detect_faces=True,
            config=config,
            on_frame=self._on_frame,
            on_error=self._on_error,
        )
        self._worker.start()

        self._patience_source = GLib.timeout_add(_TEST_PATIENCE_MS, self._on_patience_expired)
        # Escape and the close button both route through here, so the camera is
        # released however the dialog goes away.
        self.connect("closed", lambda _d: self._teardown())

    # ------------------------------------------------------------------

    def _on_frame(self, frame: PreviewFrame) -> None:
        # Dark frames of the strobe carry no picture but do carry the
        # brightness reading, which is the whole point of this test.
        texture = frame.to_texture()
        if texture is not None:
            self._preview.set_texture(texture)

        self._frames += 1
        self._brightest = max(self._brightest, frame.mean)
        self._darkest = min(self._darkest, frame.mean)
        if frame.lit:
            self._lit += 1
        else:
            self._dark += 1
        if frame.faces:
            self._faces += 1

        self._update_verdict()

    def _update_verdict(self) -> None:
        if self._camera.is_ir and self._lit == 0 and self._frames > 8:
            self._verdict.set_label(
                "Pictures are arriving, but they are all dark. The infrared "
                "light does not seem to be switching on."
            )
        elif self._faces:
            self._verdict.set_label("Working. Iris can see a face.")
        elif self._lit or not self._camera.is_ir:
            self._verdict.set_label(
                "Working. Move into the frame to check that Iris can see you."
            )
        else:
            self._verdict.set_label("Waiting for the first picture…")

        lines = [f"{self._frames} frames  ·  {self._lit} lit  ·  {self._dark} dark"]
        if self._camera.is_ir and self._frames:
            # Deliberately its own line: wrapping in the middle of "1 / 48"
            # turns a reading into two meaningless numbers.
            lines.append(
                f"brightness {self._darkest:.0f} dark / {self._brightest:.0f} lit"
            )
        self._readout.set_label("\n".join(lines))

    def _on_error(self, error: BackendError) -> None:
        self._verdict.set_label(error.message)
        self._readout.set_label(error.detail or "")
        self._preview.clear()

    def _on_patience_expired(self) -> bool:
        self._patience_source = 0
        if self._frames == 0:
            self._verdict.set_label(
                "No pictures have arrived from this camera. It may be switched "
                "off in firmware, in use by another program, or the wrong device."
            )
        return GLib.SOURCE_REMOVE

    def _teardown(self) -> None:
        if self._patience_source:
            GLib.source_remove(self._patience_source)
            self._patience_source = 0
        self._worker.stop()


# --------------------------------------------------------------------------
# shared error presentation
# --------------------------------------------------------------------------

def _show_error(
    window: "IrisWindow",
    heading: str,
    error: BackendError,
    retry: Callable[[], None] | None = None,
) -> None:
    """Report a failure with the technical detail available but not shouted.

    Every one of these dialogs offers a way forward, and none of them implies
    the user broke something -- the failures reachable from here are missing
    helpers, refused authorisations and absent hardware.
    """
    dialog = Adw.AlertDialog(heading=heading, body=error.message)
    if error.detail:
        detail = Gtk.Label(
            label=error.detail, wrap=True, xalign=0.0, selectable=True, max_width_chars=54
        )
        detail.add_css_class("iris-error-detail")
        expander = Gtk.Expander(label="Technical details")
        expander.set_child(detail)
        dialog.set_extra_child(expander)

    dialog.add_response("close", "Close")
    if retry is not None:
        dialog.add_response("retry", "Try again")
        dialog.set_response_appearance("retry", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("retry")
        dialog.connect(
            "response",
            lambda _d, response: retry() if response == "retry" else None,
        )
    else:
        dialog.set_default_response("close")
    dialog.set_close_response("close")
    dialog.present(window)
