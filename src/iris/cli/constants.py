"""Constants shared by every subcommand: exit codes, paths and timeouts."""

from __future__ import annotations


PROG = "iris"

# Exit codes.  Distinct values so shell callers can react without parsing text.
EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2          # argparse's own convention; kept for consistency
EXIT_UNAVAILABLE = 3    # irisd unreachable
EXIT_PERMISSION = 4     # needs root, or the socket refused us
EXIT_AUTH_FAILED = 5    # `iris test` ran fine but the face did not match
EXIT_INTERRUPTED = 130  # 128 + SIGINT

#: Model filenames, per SPEC.md "Paths".  Hard-coded rather than imported from
#: :mod:`iris.engine` so that ``iris doctor`` can report "models missing"
#: without first importing OpenCV (which is one of the things doctor checks).
DETECTOR_MODEL = "face_detection_yunet_2023mar.onnx"
RECOGNIZER_MODEL = "face_recognition_sface_2021dec.onnx"

#: Where the installer puts the PAM module.  Additional directories are probed
#: because the multiarch triplet differs on non-amd64 machines.
PAM_MODULE_NAME = "pam_iris.so"
PAM_MODULE_PATHS = (
    "/usr/lib/x86_64-linux-gnu/security/pam_iris.so",
    "/usr/lib/security/pam_iris.so",
    "/lib/security/pam_iris.so",
)
PAM_DIR = "/etc/pam.d"

#: SAFETY rule 2: the module must be stacked so a failure falls through to the
#: password prompt.  ``doctor`` checks the control field of every reference.
PAM_REQUIRED_CONTROL = "[success=done default=ignore]"

STORE_ROOT = "/var/lib/iris"
MASTER_KEY = "master.key"
SEALED_KEY = "master.key.tpm.priv"

TPM_DEVICE = "/dev/tpm0"
TPM_RM_DEVICE = "/dev/tpmrm0"

#: Default budget for the whole enrolment exchange.  Enrolment asks the user to
#: move their head through several poses, so it is a much longer conversation
#: than an authentication; the daemon finishes long before this fires.
ENROLL_TIMEOUT = 180.0

#: Short budget for control-plane requests (ping, list, config).
CONTROL_TIMEOUT = 5.0
