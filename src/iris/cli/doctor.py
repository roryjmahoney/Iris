"""``iris doctor``: installation diagnostics."""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import sys
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from iris import __version__
from iris import config as config_mod

from iris.cli.calibrate import run_calibration
from iris.cli.common import _socket_hint, daemon_ping, list_faces, resolve_user
from iris.cli.constants import (
    DETECTOR_MODEL,
    EXIT_FAILURE,
    EXIT_OK,
    EXIT_PERMISSION,
    MASTER_KEY,
    PAM_DIR,
    PAM_MODULE_NAME,
    PAM_MODULE_PATHS,
    PAM_REQUIRED_CONTROL,
    PROG,
    RECOGNIZER_MODEL,
    SEALED_KEY,
    STORE_ROOT,
    TPM_DEVICE,
    TPM_RM_DEVICE,
)
from iris.cli.output import CommandError, console
from iris.cli.settings import (
    _RANGES,
    _format_number,
    _safe_list_cameras,
    is_auto_device,
    load_effective_config,
    resolve_configured_device,
)


@dataclass
class Check:
    """One diagnostic result.

    *status* is ``ok`` / ``warn`` / ``fail``; only ``fail`` affects the exit
    code, so "advisory" findings (no TPM, nothing enrolled yet) can be reported
    honestly without making a healthy machine look broken to a script.
    """

    name: str
    status: str
    detail: str
    hint: str = ""
    extra: list[str] = field(default_factory=list)


def cmd_doctor(args: argparse.Namespace) -> int:
    user = resolve_user(args.user)
    cfg, config_source = load_effective_config(args.socket)

    console.print(f"{console.bold('Iris')} {__version__} — system health check")
    console.print()

    checks: list[Check] = [
        check_python_environment(),
        check_models(cfg),
        check_configuration(),
        check_store_directory(),
    ]
    checks.append(check_daemon(args.socket))
    checks.append(check_socket(args.socket))
    checks.append(check_pam())

    if args.no_camera:
        checks.append(Check("IR camera", "warn", "skipped (--no-camera)"))
        checks.append(Check("IR strobe", "warn", "skipped (--no-camera)"))
    else:
        checks.extend(check_camera(cfg, args.camera_timeout))

    checks.append(check_enrollment(user, args.socket))
    checks.append(check_tpm())

    _print_checks(checks)

    failures = sum(1 for c in checks if c.status == "fail")
    warnings = sum(1 for c in checks if c.status == "warn")
    passed = sum(1 for c in checks if c.status == "ok")

    console.print()
    summary = f"{len(checks)} checks: {passed} passed, {warnings} warnings, {failures} failed"
    if failures:
        console.print(console.red(summary))
    elif warnings:
        console.print(console.yellow(summary))
    else:
        console.print(console.green(summary))
    console.note(f"config: {config_source}")

    exit_code = EXIT_FAILURE if failures else EXIT_OK

    if args.calibrate:
        console.print()
        if failures:
            console.warn("running calibration anyway, but fix the failures above first")
        calibration_code = run_calibration(cfg, args.samples, args.calibrate_timeout)
        exit_code = exit_code or calibration_code

    return exit_code


def _print_checks(checks: Sequence[Check]) -> None:
    name_width = max(len(c.name) for c in checks)
    continuation = "  " + " " * console.status_width + "  " + " " * name_width + "  "
    arrow = "→" if console.unicode else "->"
    for check in checks:
        console.print(
            f"  {console.status_cell(check.status)}  "
            f"{check.name.ljust(name_width)}  {check.detail}"
        )
        for line in check.extra:
            console.print(continuation + console.dim(line))
        if check.hint and check.status != "ok":
            console.print(continuation + console.dim(f"{arrow} {check.hint}"))


# -- individual checks ------------------------------------------------------

def check_python_environment() -> Check:
    """Verify the apt-provided runtime dependencies actually import."""
    versions: list[str] = []
    missing: list[str] = []

    try:
        import numpy
        versions.append(f"numpy {numpy.__version__}")
    except ImportError:
        missing.append("python3-numpy")
    try:
        import cv2
        versions.append(f"OpenCV {cv2.__version__}")
    except ImportError:
        missing.append("python3-opencv")
    try:
        import cryptography
        versions.append(f"cryptography {cryptography.__version__}")
    except ImportError:
        missing.append("python3-cryptography")

    runtime = f"Python {sys.version_info.major}.{sys.version_info.minor}"
    if missing:
        return Check(
            "Python runtime",
            "fail",
            f"{runtime}; missing {', '.join(missing)}",
            hint=f"sudo apt install {' '.join(missing)}",
        )
    return Check("Python runtime", "ok", f"{runtime}, " + ", ".join(versions))


def check_models(cfg: Mapping[str, Any]) -> Check:
    configured = Path(str(cfg["recognition"]["model_dir"]))
    names = (DETECTOR_MODEL, RECOGNIZER_MODEL)
    missing = [n for n in names if not (configured / n).is_file()]

    if not missing:
        total = sum((configured / n).stat().st_size for n in names)
        return Check(
            "Models", "ok",
            f"YuNet + SFace present in {configured} ({total / 1024:.0f} KiB)",
        )

    # <repo>/src/iris/cli/doctor.py -> <repo>/models
    checkout = Path(__file__).resolve().parents[3] / "models"
    if all((checkout / n).is_file() for n in names):
        return Check(
            "Models", "warn",
            f"missing from {configured}, found in the source checkout {checkout}",
            hint=(
                "the daemon runs as root and refuses models from a user-writable "
                "directory — run the installer to copy them into "
                f"{configured}"
            ),
        )
    return Check(
        "Models", "fail",
        f"{', '.join(missing)} missing from {configured}",
        hint="run the installer (sudo ./install.sh) to install the ONNX models",
    )


def check_configuration() -> Check:
    path = Path(config_mod.CONFIG_PATH)
    try:
        info = path.stat()
    except FileNotFoundError:
        return Check(
            "Configuration", "warn",
            f"{path} does not exist; built-in defaults are in force",
            hint="run the installer, or create it with  sudo iris config set ...",
        )
    except OSError as exc:
        return Check("Configuration", "fail", f"cannot stat {path}: {exc}")

    problems: list[str] = []
    try:
        with open(path, "rb") as handle:
            raw = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        return Check(
            "Configuration", "fail",
            f"{path} is not valid TOML: {exc}",
            hint="fix the syntax; until then every setting falls back to its default",
        )
    except OSError as exc:
        return Check(
            "Configuration", "fail", f"cannot read {path}: {exc}",
            hint="the file should be 0644 root:root",
        )

    mode = stat.S_IMODE(info.st_mode)
    if mode != 0o644:
        problems.append(f"mode is {mode:04o}, expected 0644")
    if info.st_uid != 0 or info.st_gid != 0:
        problems.append(f"owned by uid {info.st_uid}:{info.st_gid}, expected root:root")

    # Compare the raw file against the schema: the loader silently repairs
    # everything below, and a typo that is silently ignored is exactly the kind
    # of thing this command exists to surface.
    for section, values in raw.items():
        if not isinstance(values, dict):
            problems.append(f"[{section}] is not a table")
            continue
        if section not in config_mod.DEFAULTS:
            problems.append(f"unknown section [{section}]")
            continue
        for key, value in values.items():
            dotted = f"{section}.{key}"
            if key not in config_mod.DEFAULTS[section]:
                problems.append(f"unknown key {dotted}")
                continue
            default = config_mod.DEFAULTS[section][key]
            if isinstance(default, bool):
                if not isinstance(value, bool):
                    problems.append(f"{dotted} should be true/false")
                continue
            if isinstance(default, int) and (isinstance(value, bool) or not isinstance(value, int)):
                problems.append(f"{dotted} should be a whole number")
                continue
            if isinstance(default, float) and (
                isinstance(value, bool) or not isinstance(value, (int, float))
            ):
                problems.append(f"{dotted} should be a number")
                continue
            if isinstance(default, str) and not isinstance(value, str):
                problems.append(f"{dotted} should be a string")
                continue
            bounds = _RANGES.get(dotted)
            if bounds and isinstance(value, (int, float)) and not isinstance(value, bool):
                if not (bounds[0] <= value <= bounds[1]):
                    problems.append(
                        f"{dotted} = {value} is outside "
                        f"{_format_number(bounds[0])}..{_format_number(bounds[1])} "
                        "and will be clamped"
                    )

    cfg = config_mod.load_config(config_mod.CONFIG_PATH)
    threshold = float(cfg["recognition"]["threshold"])
    if threshold < 0.20:
        problems.append(f"recognition.threshold = {threshold} is dangerously permissive")

    if problems:
        return Check(
            "Configuration", "warn",
            f"{path} loads, with {len(problems)} problem(s)",
            hint="every problem below falls back to the built-in default",
            extra=problems,
        )
    return Check("Configuration", "ok", f"{path} is valid (0644 root:root)")


def check_store_directory() -> Check:
    path = Path(STORE_ROOT)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return Check(
            "Template store", "warn",
            f"{path} does not exist yet",
            hint=f"it is created on first enrolment:  sudo {PROG} enroll",
        )
    except OSError as exc:
        return Check("Template store", "fail", f"cannot stat {path}: {exc}")

    if stat.S_ISLNK(info.st_mode):
        return Check(
            "Template store", "fail",
            f"{path} is a symlink",
            hint="the store refuses to follow it; remove the link and re-enrol",
        )
    if not stat.S_ISDIR(info.st_mode):
        return Check("Template store", "fail", f"{path} is not a directory")

    mode = stat.S_IMODE(info.st_mode)
    problems = []
    if mode != 0o700:
        problems.append(f"mode is {mode:04o}, expected 0700")
    if info.st_uid != 0 or info.st_gid != 0:
        problems.append(f"owned by {info.st_uid}:{info.st_gid}, expected root:root")
    if problems:
        return Check(
            "Template store", "fail",
            f"{path}: " + "; ".join(problems),
            hint=f"sudo chown root:root {path} && sudo chmod 700 {path}",
        )
    return Check("Template store", "ok", f"{path} is root:root 0700")


def check_daemon(socket_path: str) -> Check:
    reply = daemon_ping(socket_path, timeout=3.0)
    if reply is None:
        # Not being allowed to *try* is different from the daemon being down,
        # and reporting an unprivileged run as a failure would make `iris
        # doctor` exit non-zero on a perfectly healthy machine.
        if os.geteuid() != 0 and os.path.exists(socket_path):
            return Check(
                "Daemon", "warn",
                "cannot be checked without root (the socket is 0600 root:root)",
                hint=f"re-run as  sudo {PROG} doctor",
            )
        return Check(
            "Daemon", "fail",
            f"not reachable on {socket_path}",
            hint=_socket_hint(socket_path),
        )

    version = reply.get("version")
    if isinstance(version, str) and version != __version__:
        return Check(
            "Daemon", "warn",
            f"running version {version}, but this CLI is {__version__}",
            hint="restart the service after upgrading:  sudo systemctl restart irisd",
        )
    return Check(
        "Daemon", "ok", f"running and answering on {socket_path} (version {version})"
    )


def check_socket(socket_path: str) -> Check:
    try:
        info = os.stat(socket_path)
    except FileNotFoundError:
        return Check(
            "Socket permissions", "fail",
            f"{socket_path} does not exist",
            hint="irisd creates it on start:  sudo systemctl start irisd",
        )
    except PermissionError:
        return Check(
            "Socket permissions", "warn",
            f"cannot inspect {socket_path} without root",
            hint=f"re-run as  sudo {PROG} doctor",
        )
    except OSError as exc:
        return Check("Socket permissions", "fail", f"cannot stat {socket_path}: {exc}")

    if not stat.S_ISSOCK(info.st_mode):
        return Check(
            "Socket permissions", "fail",
            f"{socket_path} is not a socket",
            hint="delete the stale file and restart irisd",
        )

    mode = stat.S_IMODE(info.st_mode)
    problems = []
    if mode != 0o600:
        problems.append(f"mode {mode:04o}, expected 0600")
    if info.st_uid != 0 or info.st_gid != 0:
        problems.append(f"owner {info.st_uid}:{info.st_gid}, expected root:root")
    if problems:
        return Check(
            "Socket permissions", "fail",
            "; ".join(problems),
            hint=(
                "a socket any user can write to lets unprivileged processes drive "
                "enrolment and authentication; fix the daemon's umask/unit file"
            ),
        )
    return Check("Socket permissions", "ok", f"{socket_path} is root:root 0600")


def _auth_fallback_missing(path: str) -> bool:
    """True if *path* stacks pam_iris on ``auth`` with nothing to fall back to.

    This catches the single most plausible way to lock yourself out of this
    machine.  The documented edit is to ADD the pam_iris line *above* the
    existing password module::

        auth  [success=done default=ignore]  pam_iris.so
        auth  ...                            pam_unix.so       <-- must remain

    The mistake is to REPLACE that second line instead — the file still looks
    right, `iris doctor` still sees the correct control value, and face auth
    still works, so nothing complains.  But ``default=ignore`` now falls
    through to an empty stack: cover the camera, or stop the daemon, and there
    is no password prompt left to answer.  On ``gdm-password`` that is a
    locked-out desktop; on ``sudo`` it is a machine you can no longer
    administer.

    A file is considered to have a fallback if it has any other ``auth`` line
    (a module or an ``@include``), which is the only thing that can supply
    one.  Unreadable or unparseable files are reported as fine — this is a
    warning aid, and a false alarm on every run trains people to ignore it.
    """
    has_iris_auth = False
    has_other_auth = False

    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for raw in handle:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue

                # "@include common-auth" pulls in a whole stack, password
                # module included; treat it as a fallback.
                if line.startswith("@include"):
                    has_other_auth = True
                    continue

                fields = line.split()
                if len(fields) < 2 or fields[0].lower() != "auth":
                    continue

                if PAM_MODULE_NAME in line:
                    has_iris_auth = True
                else:
                    has_other_auth = True
    except OSError:
        return False

    return has_iris_auth and not has_other_auth


def check_pam() -> Check:
    installed = [p for p in PAM_MODULE_PATHS if os.path.isfile(p)]
    if not installed:
        # Probe other multiarch triplets before declaring it missing.
        for candidate in sorted(Path("/usr/lib").glob(f"*/security/{PAM_MODULE_NAME}")):
            installed.append(str(candidate))

    references: list[tuple[str, str]] = []
    unreadable = False
    try:
        entries = sorted(os.listdir(PAM_DIR))
    except OSError:
        entries = []
        unreadable = True

    for entry in entries:
        path = os.path.join(PAM_DIR, entry)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    stripped = line.strip()
                    if stripped.startswith("#") or PAM_MODULE_NAME not in stripped:
                        continue
                    references.append((entry, stripped))
        except OSError:
            unreadable = True

    if not installed and not references:
        return Check(
            "PAM module", "warn",
            f"{PAM_MODULE_NAME} is not installed and not referenced",
            hint=(
                "face unlock for login/sudo/polkit needs it — run the installer; "
                "password authentication is unaffected"
            ),
        )
    if not installed and references:
        files = ", ".join(sorted({name for name, _ in references}))
        return Check(
            "PAM module", "fail",
            f"referenced by {files} but {PAM_MODULE_NAME} is not installed",
            hint=(
                "PAM logs a module-unknown error on every login; either install "
                "the module or remove those lines"
            ),
        )
    if installed and not references:
        return Check(
            "PAM module", "warn",
            f"installed at {installed[0]} but not referenced in {PAM_DIR}",
            hint=(
                "nothing will ever call it; the installer adds an "
                f"'auth {PAM_REQUIRED_CONTROL} {PAM_MODULE_NAME}' line"
            ),
        )

    # Collapse runs of whitespace before matching: PAM files are conventionally
    # column-aligned with tabs and multiple spaces, and the control field is
    # written as one token there but with single spaces in our constant.
    unsafe = [
        (name, line)
        for name, line in references
        if PAM_REQUIRED_CONTROL not in " ".join(line.split())
    ]
    files = ", ".join(sorted({name for name, _ in references}))
    if unsafe:
        # SAFETY rule 2: any other control (required/sufficient) either blocks
        # the password fallback or grants access on module success without the
        # "stop here" semantics the design assumes.
        bad_files = ", ".join(sorted({name for name, _ in unsafe}))
        return Check(
            "PAM module", "fail",
            f"stacked without '{PAM_REQUIRED_CONTROL}' in {bad_files}",
            hint=(
                "face auth must fall through to the password prompt on failure; "
                f"use  auth {PAM_REQUIRED_CONTROL} {PAM_MODULE_NAME}"
            ),
            extra=[f"{name}: {line}" for name, line in unsafe],
        )

    # SAFETY rule 5: the control value being right is not enough — there has to
    # be something left to fall through TO.  Checked after the control value so
    # the more common mistake is reported first.
    stranded = sorted({
        name for name, _ in references
        if _auth_fallback_missing(os.path.join(PAM_DIR, name))
    })
    if stranded:
        return Check(
            "PAM module", "fail",
            f"{PAM_MODULE_NAME} is the only auth module in {', '.join(stranded)}",
            hint=(
                "there is no password fallback in that stack — if the camera "
                "fails you cannot authenticate at all; restore the pam_unix.so "
                "line (or '@include common-auth') BELOW the pam_iris.so line, "
                "from a root shell you still have open"
            ),
            extra=[f"{name}: no other auth line" for name in stranded],
        )

    detail = f"{installed[0]}, referenced by {files}"
    if unreadable:
        return Check(
            "PAM module", "warn",
            detail + f" (some files in {PAM_DIR} were unreadable)",
            hint=f"re-run as  sudo {PROG} doctor  for a complete answer",
        )
    return Check("PAM module", "ok", detail)


def check_camera(cfg: Mapping[str, Any], timeout: float) -> list[Check]:
    """Open the configured IR device and look for the emitter's strobe.

    Returns two checks (device, strobe) because they fail for different reasons
    and need different remediation, but they share one capture: opening the IR
    camera twice costs a second and briefly fights the daemon for the device.
    """
    configured = str(cfg["camera"]["device"])
    min_brightness = float(cfg["camera"]["min_frame_brightness"])

    resolved = resolve_configured_device(configured)
    if resolved is None:
        return [
            Check(
                "IR camera", "fail",
                "camera.device is 'auto' but no infrared camera was recognised",
                hint=(
                    f"run  {PROG} cameras  to see this machine's devices; if the laptop "
                    f"has an IR camera, run  {PROG} hardware-report  and open a hardware "
                    "issue so support can be added"
                ),
            ),
            Check("IR strobe", "warn", "skipped (camera unavailable)"),
        ]
    device = resolved
    # Open exactly the node that was checked, even if a hot-plug renumbers
    # /dev/video* between here and the capture.
    cfg = {**cfg, "camera": {**cfg["camera"], "device": device}}

    if not os.path.exists(device):
        available = [c["path"] for c in _safe_list_cameras() if c.get("is_ir")]
        hint = (
            f"an infrared camera was detected at {available[0]}; let Iris pick it with  "
            f"sudo {PROG} config set camera.device auto"
            if available
            else f"run  {PROG} cameras  to see what this machine has"
        )
        return [
            Check("IR camera", "fail", f"{device} does not exist", hint=hint),
            Check("IR strobe", "warn", "skipped (camera unavailable)"),
        ]

    for camera in _safe_list_cameras():
        if camera.get("path") == device and camera.get("is_metadata"):
            return [
                Check(
                    "IR camera", "fail",
                    f"{device} is a V4L2 metadata node, not a capture device",
                    hint=f"run  {PROG} cameras  and pick a node marked IR",
                ),
                Check("IR strobe", "warn", "skipped (camera unavailable)"),
            ]

    try:
        from iris.camera import Camera, CameraError
    except ImportError as exc:
        return [
            Check("IR camera", "fail", f"cannot import iris.camera: {exc}",
                  hint="sudo apt install python3-opencv"),
            Check("IR strobe", "warn", "skipped (camera unavailable)"),
        ]

    means: list[float] = []
    started = time.monotonic()
    try:
        with Camera.from_config(dict(cfg)) as cam:
            # raw_frames(), not frames(): the dark half of the strobe is exactly
            # what the second check is looking for, and frames() drops it.
            for frame in cam.raw_frames(timeout):
                means.append(float(frame.mean()))
    except CameraError as exc:
        message = str(exc)
        hint = f"run  {PROG} cameras  to see the available devices"
        if "Device or resource busy" in message or "busy" in message.lower():
            hint = (
                "another process holds the camera — irisd may be mid-"
                "authentication; try again in a moment"
            )
        elif "permission" in message.lower():
            hint = (
                f"you need read access to {device}: check the ACL "
                f"(getfacl {device}) or add yourself to the 'video' group"
            )
        return [
            Check("IR camera", "fail", message, hint=hint),
            Check("IR strobe", "warn", "skipped (camera could not be opened)"),
        ]
    except Exception as exc:  # noqa: BLE001 - diagnostics must not traceback
        return [
            Check("IR camera", "fail", f"unexpected camera failure: {exc}"),
            Check("IR strobe", "warn", "skipped (camera could not be opened)"),
        ]

    elapsed = time.monotonic() - started
    if not means:
        return [
            Check(
                "IR camera", "fail",
                f"{device} opened but delivered no frames in {elapsed:.1f}s",
                hint="the device may be a metadata node or held by another process",
            ),
            Check("IR strobe", "warn", "skipped (no frames)"),
        ]

    lit = [m for m in means if m >= min_brightness]
    dark = [m for m in means if m < min_brightness]
    fps = len(means) / elapsed if elapsed > 0 else 0.0
    device_check = Check(
        "IR camera", "ok",
        f"{'auto → ' if is_auto_device(configured) else ''}{device}: "
        f"{len(means)} frames in {elapsed:.1f}s ({fps:.1f} fps), "
        f"{cfg['camera']['width']}x{cfg['camera']['height']}",
    )

    if lit and dark:
        strobe = Check(
            "IR strobe", "ok",
            f"emitter strobing: {len(lit)} lit (mean {sum(lit) / len(lit):.1f}) / "
            f"{len(dark)} dark (mean {sum(dark) / len(dark):.1f}), "
            f"threshold {min_brightness:.1f}",
        )
    elif lit:
        strobe = Check(
            "IR strobe", "warn",
            f"every frame was lit (mean {sum(lit) / len(lit):.1f}); the emitter "
            "does not appear to strobe on this device",
            hint=(
                "this is harmless — no frames are discarded — but it means "
                "min_frame_brightness is doing nothing"
            ),
        )
    else:
        brightest = max(means)
        strobe = Check(
            "IR strobe", "fail",
            f"all {len(means)} frames were below min_frame_brightness="
            f"{min_brightness:.1f} (brightest {brightest:.1f})",
            hint=(
                "the infrared emitter is not firing, or the threshold is too "
                f"high: sudo {PROG} config set camera.min_frame_brightness "
                f"{max(1.0, brightest / 2):.0f}. Many laptops need the emitter "
                "enabled first (e.g. linux-enable-ir-emitter); if that does not "
                f"help, run  {PROG} hardware-report  and open a hardware issue"
            ),
        )
    return [device_check, strobe]


def check_enrollment(user: str, socket_path: str) -> Check:
    try:
        faces, source = list_faces(user, socket_path)
    except CommandError as exc:
        if exc.code == EXIT_PERMISSION or os.geteuid() != 0:
            return Check(
                "Enrollment", "warn",
                f"cannot be read without root ({user})",
                hint=f"re-run as  sudo {PROG} doctor",
            )
        return Check("Enrollment", "fail", str(exc), hint=exc.hint)

    if not faces:
        return Check(
            "Enrollment", "warn",
            f"no faces enrolled for {user}",
            hint=f"enrol one with:  sudo {PROG} enroll --user {user}",
        )

    total = sum(int(f.get("samples", 0) or 0) for f in faces)
    names = ", ".join(str(f.get("name", "?")) for f in faces)
    detail = f"{len(faces)} face(s) for {user}: {names} ({total} samples)"
    if source == "store":
        detail += " [read from disk; daemon down]"
    if total < 3:
        return Check(
            "Enrollment", "warn", detail,
            hint="that is very few samples; re-enrol for more reliable matching",
        )
    return Check("Enrollment", "ok", detail)


def check_tpm() -> Check:
    try:
        from iris.store import tpm_available
    except ImportError:
        tpm_available = None  # type: ignore[assignment]

    has_raw = os.path.exists(TPM_DEVICE)
    has_rm = os.path.exists(TPM_RM_DEVICE)
    tools = [t for t in ("tpm2_createprimary", "tpm2_create", "tpm2_load", "tpm2_unseal")
             if shutil.which(t) is None]

    sealed = (Path(STORE_ROOT) / SEALED_KEY).exists()
    plain = (Path(STORE_ROOT) / MASTER_KEY).exists()

    if not has_raw and not has_rm:
        return Check(
            "TPM", "warn",
            "no TPM device; the master key is a plain 0600 root-only file",
            hint="templates are still encrypted, just not bound to this machine",
        )
    if not has_rm:
        return Check(
            "TPM", "warn",
            f"{TPM_DEVICE} exists but {TPM_RM_DEVICE} does not",
            hint=(
                "Iris only uses the kernel resource manager; load the tpm_crb/"
                "tpm_tis driver or start tpm2-abrmd"
            ),
        )
    if tools:
        return Check(
            "TPM", "warn",
            f"{TPM_RM_DEVICE} present but tpm2-tools missing: {', '.join(tools)}",
            hint="sudo apt install tpm2-tools",
        )

    usable = tpm_available() if tpm_available is not None else True
    if not usable:
        return Check(
            "TPM", "warn",
            f"{TPM_RM_DEVICE} present but unusable",
            hint="check permissions on the resource-manager node",
        )
    if sealed:
        return Check("TPM", "ok", f"available; the master key is sealed to it ({TPM_RM_DEVICE})")
    if plain:
        return Check(
            "TPM", "warn",
            "available, but the master key is a plain file",
            hint=(
                "the key predates the TPM being usable; to bind it, clear and "
                f"re-enrol:  sudo {PROG} clear && sudo {PROG} enroll"
            ),
        )
    return Check("TPM", "ok", f"available ({TPM_RM_DEVICE}); the key will be sealed on first enrolment")
