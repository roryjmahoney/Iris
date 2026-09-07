"""The Iris application: window, navigation, welcome screen and entry point.

Structure
---------
One :class:`Adw.ApplicationWindow` holding an :class:`Adw.NavigationView` whose
root is the welcome screen; Enrol, Your Faces and Settings are pushed onto it.
A navigation stack rather than a tab strip because these are *destinations you
come back from*, not sections you switch between: the back gesture, the back
button and Escape all mean the same thing on every screen, and the header title
always names where you are.

Theming
-------
Light and dark come from :class:`Adw.StyleManager`, which is the only source
that accounts for the desktop's colour-scheme portal, an application override
and the forced-light/forced-dark cases together.  The window mirrors its
``dark`` property onto an ``iris-dark`` CSS class on itself; because the window
is the CSS ``:root``, the custom properties that class redefines cascade to
every widget in the window, dialogs included.

Nothing in this module opens a camera or touches privileged state.  Those live
behind :mod:`iris.gui.backend`, which keeps them on worker threads.
"""

from __future__ import annotations

import logging
import sys
from typing import Any, Final

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from iris import __version__

from . import APP_ID, backend
from .enroll import EnrollPage
from .settings import FacesPage, SettingsPage
from .widgets import FaceDial

_LOG = logging.getLogger("iris.gui.app")

#: Below this the window is one narrow column: the enrolment portrait shrinks
#: and slider marks lose their labels.  Matches libadwaita's own convention of
#: switching to a compact layout at around a 600 px content width.
_COMPACT_CONDITION: Final[str] = "max-width: 560px"

#: Comfortable on a 2560x1600 panel without being a full-screen takeover.
_DEFAULT_WIDTH: Final[int] = 940
_DEFAULT_HEIGHT: Final[int] = 720

#: The floor the layout is designed to survive, not a suggestion: every page
#: scrolls vertically and no page scrolls horizontally at this size.
_MIN_WIDTH: Final[int] = 360
_MIN_HEIGHT: Final[int] = 420


# --------------------------------------------------------------------------
# Welcome
# --------------------------------------------------------------------------

class WelcomePage(Adw.NavigationPage):
    """The root screen: what Iris is, what state it is in, and one clear action."""

    __gtype_name__ = "IrisWelcomePage"

    def __init__(self, window: "IrisWindow") -> None:
        super().__init__(title="Iris")
        self._window = window

        header = Adw.HeaderBar()
        header.pack_end(self._build_menu_button())

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(self._build_body())
        self.set_child(toolbar)

        self.connect("showing", lambda _p: self.refresh())

    def _build_menu_button(self) -> Gtk.MenuButton:
        menu = Gio.Menu()
        menu.append("Settings", "win.settings")
        menu.append("Your Faces", "win.faces")
        section = Gio.Menu()
        section.append("Keyboard Shortcuts", "win.shortcuts")
        section.append("About Iris", "app.about")
        menu.append_section(None, section)

        button = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu)
        button.set_tooltip_text("Main menu")
        button.update_property([Gtk.AccessibleProperty.LABEL], ["Main menu"])
        return button

    def _build_body(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)
        box.add_css_class("iris-page")
        box.set_valign(Gtk.Align.CENTER)

        # Product mark: use Iris's own biometric dial, not a generic camera
        # glyph. The quiet idle wave identifies the feature without implying
        # that the camera is currently recording.
        mark = FaceDial(natural_size=132, label="Iris face authentication")
        mark.set_halign(Gtk.Align.CENTER)
        mark.set_valign(Gtk.Align.CENTER)
        mark.add_css_class("iris-hero-dial")
        mark.set_idle()

        title = Gtk.Label(label="Iris", halign=Gtk.Align.CENTER)
        title.add_css_class("iris-display")

        subtitle = Gtk.Label(
            label="Sign in by looking at your laptop.",
            halign=Gtk.Align.CENTER,
            wrap=True,
            justify=Gtk.Justification.CENTER,
        )
        subtitle.add_css_class("iris-lede")

        self._status_pill = Gtk.Label(label="Checking…", halign=Gtk.Align.CENTER)
        self._status_pill.add_css_class("iris-pill")
        self._status_pill.set_accessible_role(Gtk.AccessibleRole.STATUS)

        self._primary = Gtk.Button(label="Set up Face Unlock", halign=Gtk.Align.CENTER)
        self._primary.add_css_class("iris-cta")
        self._primary.add_css_class("suggested-action")
        self._primary.connect("clicked", lambda _b: self._window.open_enroll())

        group = Adw.PreferencesGroup(margin_top=16)

        self._faces_row = Adw.ActionRow(
            title="Your faces",
            subtitle="See what is enrolled and remove it",
            activatable=True,
        )
        self._faces_row.add_prefix(Gtk.Image(icon_name="avatar-default-symbolic"))
        self._faces_row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        self._faces_row.connect("activated", lambda _r: self._window.open_faces())
        group.add(self._faces_row)

        settings_row = Adw.ActionRow(
            title="Settings",
            subtitle="Camera, strictness and timeouts",
            activatable=True,
        )
        settings_row.add_prefix(Gtk.Image(icon_name="preferences-system-symbolic"))
        settings_row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        settings_row.connect("activated", lambda _r: self._window.open_settings())
        group.add(settings_row)

        footnote = Gtk.Label(
            label=(
                "Iris keeps an encrypted mathematical model of your face on this "
                "computer. It never keeps pictures, and nothing is sent anywhere. "
                "Your password keeps working exactly as it does now."
            ),
            halign=Gtk.Align.CENTER,
            justify=Gtk.Justification.CENTER,
            wrap=True,
            max_width_chars=52,
            margin_top=8,
        )
        footnote.add_css_class("iris-caption")

        box.append(mark)
        box.append(title)
        box.append(subtitle)
        box.append(self._status_pill)
        box.append(self._primary)
        box.append(group)
        box.append(footnote)

        scroller = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            vexpand=True,
        )
        scroller.set_child(Adw.Clamp(maximum_size=560, tightening_threshold=440, child=box))
        return scroller

    # ------------------------------------------------------------------

    def refresh(self) -> None:
        """Re-read the state Iris is in and restate it in one line.

        The face list is read without escalating: if it needs a password we
        simply do not know the count, and the screen says what it does know
        rather than firing a polkit prompt at someone who only opened a window.
        """
        cfg = backend.load_config()
        enabled = bool(cfg.get("auth", {}).get("enabled", True))
        user = backend.current_user()

        backend.run_async(
            lambda: backend.fetch_faces(user, allow_prompt=False),
            lambda faces, error: self._apply_state(enabled, faces, error),
        )

    def _apply_state(
        self, enabled: bool, faces: Any, error: backend.BackendError | None
    ) -> None:
        for css_class in ("iris-ok", "iris-warn", "iris-danger"):
            self._status_pill.remove_css_class(css_class)

        if error is not None:
            # The count is unknown, not zero, so the pill states only what we
            # actually know -- whether face unlock is switched on -- rather
            # than claiming an enrolment that may or may not exist.
            if not error.retryable:
                self._status_pill.set_label(error.message)
                self._status_pill.add_css_class("iris-warn")
                self._faces_row.set_subtitle(error.detail.splitlines()[0] if error.detail else "")
            else:
                self._status_pill.set_label(
                    "Face unlock is turned on" if enabled else "Face unlock is turned off"
                )
                self._status_pill.add_css_class("iris-ok" if enabled else "iris-warn")
                self._faces_row.set_subtitle("Unlock to see what is enrolled")
            self._primary.set_label("Set up Face Unlock")
            return

        count = len(faces)

        if not enabled:
            self._status_pill.set_label("Face unlock is turned off")
            self._status_pill.add_css_class("iris-warn")
        elif count:
            self._status_pill.set_label("Ready — face unlock is on")
            self._status_pill.add_css_class("iris-ok")
        else:
            self._status_pill.set_label("Not set up yet")

        self._primary.set_label("Add another face" if count else "Set up Face Unlock")
        self._faces_row.set_subtitle(
            "Nothing enrolled yet" if not count else
            f"{count} face{'s' if count != 1 else ''} enrolled"
        )


# --------------------------------------------------------------------------
# Window
# --------------------------------------------------------------------------

class IrisWindow(Adw.ApplicationWindow):
    """The single window.  Owns navigation, toasts and the compact breakpoint."""

    __gtype_name__ = "IrisWindow"

    def __init__(self, application: Adw.Application) -> None:
        super().__init__(
            application=application,
            title="Iris",
            default_width=_DEFAULT_WIDTH,
            default_height=_DEFAULT_HEIGHT,
        )
        self.add_css_class("iris-window")
        self.set_size_request(_MIN_WIDTH, _MIN_HEIGHT)

        self._navigation = Adw.NavigationView()
        self._welcome = WelcomePage(self)
        self._navigation.add(self._welcome)

        # Built lazily: Settings enumerates cameras (which imports OpenCV) and
        # Enrol builds a live preview, neither of which should be paid for by
        # someone who opens the window and closes it again.
        self._enroll_page: EnrollPage | None = None
        self._settings_page: SettingsPage | None = None
        self._faces_page: FacesPage | None = None

        self._toasts = Adw.ToastOverlay(child=self._navigation)
        self.set_content(self._toasts)

        self._install_actions()
        self._install_breakpoint()
        self._follow_colour_scheme()

    # -- theming ---------------------------------------------------------

    def _follow_colour_scheme(self) -> None:
        manager = Adw.StyleManager.get_default()
        manager.connect("notify::dark", lambda *_a: self._sync_dark())
        self._sync_dark()

    def _sync_dark(self) -> None:
        if Adw.StyleManager.get_default().get_dark():
            self.add_css_class("iris-dark")
        else:
            self.remove_css_class("iris-dark")

    # -- adaptivity ------------------------------------------------------

    def _install_breakpoint(self) -> None:
        breakpoint_ = Adw.Breakpoint.new(
            Adw.BreakpointCondition.parse(_COMPACT_CONDITION)
        )
        breakpoint_.connect("apply", lambda _b: self._set_compact(True))
        breakpoint_.connect("unapply", lambda _b: self._set_compact(False))
        self.add_breakpoint(breakpoint_)

    def _set_compact(self, compact: bool) -> None:
        """Tell every built page to lay itself out for a narrow window."""
        if compact:
            self.add_css_class("iris-compact")
        else:
            self.remove_css_class("iris-compact")
        for page in (self._enroll_page, self._settings_page, self._faces_page):
            setter = getattr(page, "set_compact", None)
            if setter is not None:
                setter(compact)

    # -- actions and shortcuts -------------------------------------------

    def _install_actions(self) -> None:
        for name, handler in (
            ("enroll", lambda *_a: self.open_enroll()),
            ("settings", lambda *_a: self.open_settings()),
            ("faces", lambda *_a: self.open_faces()),
            ("shortcuts", lambda *_a: self._show_shortcuts()),
            ("close", lambda *_a: self.close()),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", handler)
            self.add_action(action)

    def _show_shortcuts(self) -> None:
        dialog = Adw.AlertDialog(
            heading="Keyboard shortcuts",
            body=(
                "Ctrl+N — set up a face\n"
                "Ctrl+, — settings\n"
                "Escape — go back, or cancel what is running\n"
                "Alt+← — go back\n"
                "Ctrl+W — close the window\n"
                "Ctrl+Q — quit\n\n"
                "Tab and Shift+Tab move between controls; Space or Return "
                "activates the focused one."
            ),
        )
        dialog.add_response("close", "Close")
        dialog.set_default_response("close")
        dialog.set_close_response("close")
        dialog.present(self)

    # -- navigation -------------------------------------------------------

    def open_enroll(self) -> None:
        if self._enroll_page is None:
            self._enroll_page = EnrollPage(self)
            self._enroll_page.set_compact(self.has_css_class("iris-compact"))
        # The page is reused across visits, so it has to be put back to its
        # first screen every time -- otherwise reopening it lands on last
        # visit's result, with a dead camera behind it.
        self._enroll_page.reset()
        self._push(self._enroll_page)

    def open_settings(self) -> None:
        if self._settings_page is None:
            self._settings_page = SettingsPage(self)
            self._settings_page.set_compact(self.has_css_class("iris-compact"))
        self._push(self._settings_page)

    def open_faces(self) -> None:
        if self._faces_page is None:
            self._faces_page = FacesPage(self)
        self._push(self._faces_page)

    def _push(self, page: Adw.NavigationPage) -> None:
        """Push *page*, or return to it if it is already on the stack.

        Pushing a page twice is a programming error in AdwNavigationView (it
        warns and refuses), and it is reachable here: Settings can be opened
        from Enrol, which was itself opened from the welcome screen.
        """
        if page in self._navigation.get_navigation_stack():
            self._navigation.pop_to_page(page)
            return
        self._navigation.push(page)

    def pop_page(self) -> None:
        """Go back one page, or do nothing if we are already at the root."""
        self._navigation.pop()

    # -- services offered to pages ----------------------------------------

    def reload_config(self) -> None:
        """The configuration on disk changed; restate the summary."""
        self._welcome.refresh()

    def invalidate_faces(self) -> None:
        """The set of enrolled faces changed; anything showing it should re-read."""
        self._welcome.refresh()

    def toast(self, message: str) -> None:
        """Brief, non-blocking confirmation.  Never used to report a failure."""
        self._toasts.add_toast(Adw.Toast(title=message, timeout=3))


# --------------------------------------------------------------------------
# Application
# --------------------------------------------------------------------------

class IrisApplication(Adw.Application):
    """The GApplication.  Single-instance: activating again raises the window."""

    __gtype_name__ = "IrisApplication"

    def __init__(self) -> None:
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
        )
        self._window: IrisWindow | None = None

        about = Gio.SimpleAction.new("about", None)
        about.connect("activate", lambda *_a: self._show_about())
        self.add_action(about)

        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_a: self.quit())
        self.add_action(quit_action)

        self.set_accels_for_action("app.quit", ["<primary>q"])
        self.set_accels_for_action("win.close", ["<primary>w"])
        self.set_accels_for_action("win.enroll", ["<primary>n"])
        self.set_accels_for_action("win.settings", ["<primary>comma"])
        self.set_accels_for_action("win.faces", ["<primary>f"])
        self.set_accels_for_action("win.shortcuts", ["<primary>question"])

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        _load_stylesheet()
        Gtk.Window.set_default_icon_name(_app_icon_name())

    def do_activate(self) -> None:
        if self._window is None:
            self._window = IrisWindow(self)
        self._window.present()

    def _show_about(self) -> None:
        about = Adw.AboutDialog(
            application_name="Iris",
            application_icon=_app_icon_name(),
            version=__version__,
            developer_name="Iris",
            comments=(
                "Infrared face authentication for this computer.\n\n"
                "Iris stores an encrypted mathematical model of your face — never "
                "a picture — and only on this machine. Password sign-in keeps "
                "working everywhere it does today."
            ),
            license_type=Gtk.License.AGPL_3_0_ONLY,
        )
        about.add_credit_section("Built on", [
            "OpenCV YuNet face detection",
            "OpenCV SFace recognition",
            "GTK4 and libadwaita",
        ])
        about.present(self._window)


def _app_icon_name() -> str:
    """``APP_ID`` if the installer shipped an icon, otherwise a stock one.

    Naming an icon that is not in the theme renders as the broken-image glyph,
    which looks like a bug in the About dialog and in the window switcher. The
    app has to run correctly from a source checkout too, where no icon has been
    installed yet, so this checks rather than assuming.
    """
    display = Gdk.Display.get_default()
    if display is not None and Gtk.IconTheme.get_for_display(display).has_icon(APP_ID):
        return APP_ID
    return "camera-web-symbolic"


def _load_stylesheet() -> None:
    """Load ``style.css`` and add it above libadwaita's own stylesheet.

    Failing to find or parse the stylesheet is survivable: the app falls back
    to plain libadwaita styling, which is unremarkable but perfectly usable.
    Refusing to start over a missing CSS file would not be.
    """
    css = _read_stylesheet()
    if css is None:
        _LOG.warning("style.css not found; falling back to default Adwaita styling")
        return

    provider = Gtk.CssProvider()
    problems: list[str] = []
    provider.connect(
        "parsing-error",
        lambda _p, section, error: problems.append(
            f"line {section.get_start_location().lines + 1}: {error.message}"
        ),
    )
    provider.load_from_string(css)
    for problem in problems:
        _LOG.error("style.css: %s", problem)

    display = Gdk.Display.get_default()
    if display is None:  # pragma: no cover - no display means no GUI at all
        _LOG.error("no display; stylesheet not applied")
        return

    # PRIORITY_APPLICATION sits above libadwaita's theme and below any user
    # stylesheet, which is the correct rung for an application's own design.
    Gtk.StyleContext.add_provider_for_display(
        display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )


def _read_stylesheet() -> str | None:
    """Find ``style.css`` next to this module, or where the installer put it."""
    from pathlib import Path

    candidates = [
        Path(__file__).with_name("style.css"),
        Path("/usr/share/iris/style.css"),
    ]
    for path in candidates:
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            continue
    return None


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    """Run the Iris desktop application.

    :param argv: command line, defaulting to :data:`sys.argv`.
    :returns: the process exit status.
    """
    argv = list(sys.argv if argv is None else argv)

    if "--version" in argv:
        print(f"iris {__version__}")
        return 0

    logging.basicConfig(
        level=logging.DEBUG if "--debug" in argv else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    # GApplication would try to parse it and fail; it is ours, not GTK's.
    argv = [arg for arg in argv if arg != "--debug"]

    GLib.set_application_name("Iris")
    GLib.set_prgname(APP_ID)

    return IrisApplication().run(argv)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
