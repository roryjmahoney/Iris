# Iris — Internal Interface Contract (v1.0.0)

Authoritative module boundaries. Every component MUST match these signatures exactly.
Hardware facts verified on the target machine (Ubuntu 26.04, GNOME 50.1, Wayland):

- IR camera: `/dev/video2` "LGE IR-FHD Camera", **GREY 8-bit, 640x360, 15fps only**.
- RGB camera: `/dev/video0` (MJPG/YUYV). `/dev/video1`,`/dev/video3` are METADATA nodes — never open them.
- **The IR emitter strobes.** Alternating frames are dark (mean~1) and lit (mean~55-62).
  Frames with `mean < min_frame_brightness` MUST be discarded before detection.
- User has ACL rw on /dev/video*; TPM present at /dev/tpm0.
- OpenCV 4.10 (apt), numpy 2.3.5, Python 3.14. No dlib, no pip installs.

## Paths
    /usr/share/iris/models/{face_detection_yunet_2023mar.onnx,face_recognition_sface_2021dec.onnx}
    /etc/iris/config.toml            root:root 0644
    /var/lib/iris/                   root:root 0700   (templates + master.key)
    /run/irisd/socket                root:root 0600   (SOCK_STREAM)
    /usr/lib/x86_64-linux-gnu/security/pam_iris.so

## iris/config.py
    CONFIG_PATH = "/etc/iris/config.toml"
    DEFAULTS = {
      "camera":      {"device":"/dev/video2","width":640,"height":360,
                      "ir_mode":True,"min_frame_brightness":20.0},
      "recognition": {"threshold":0.363,"detect_score":0.7,
                      "model_dir":"/usr/share/iris/models",
                      "required_matches":3,"max_frames":120},
      "auth":        {"enabled":True,"timeout":8.0,"max_failures":5,"lockout_seconds":60},
      "liveness":    {"enabled":True,"min_variance":12.0},
    }
    load_config(path=CONFIG_PATH) -> dict     # deep-merged over DEFAULTS, never raises
    save_config(cfg, path=CONFIG_PATH) -> None  # atomic write, emits valid TOML
Read with `tomllib`. Writing: hand-rolled flat emitter (schema is 2 levels, str/int/float/bool only).

## iris/camera.py
    list_cameras() -> list[dict]   # {"path","name","is_ir","is_metadata","formats":[str]}
                                   # MUST exclude metadata nodes from is_ir/usable results
    class Camera:
        def __init__(self, device, width, height, min_brightness=20.0, ir_mode=True)
        def __enter__/__exit__                      # releases VideoCapture
        def frames(self, timeout) -> Iterator[np.ndarray]
            # yields ONLY illuminated 2-D uint8 grayscale frames (mean >= min_brightness
            # when ir_mode); stops after `timeout` seconds; never blocks forever.
        def raw_frames(self, timeout) -> Iterator[np.ndarray]  # preview: no brightness filter, BGR

## iris/engine.py
    class FaceEngine:
        def __init__(self, cfg: dict)          # loads YuNet + SFace from cfg model_dir
        def detect(self, gray) -> np.ndarray|None   # YuNet rows; input 2-D gray, CLAHE applied internally
        def embed(self, gray, face_row) -> np.ndarray  # SFace feature vector (float32)
        @staticmethod
        def compare(a, b) -> float             # cosine similarity in [-1,1]
    Match rule: cosine >= cfg["recognition"]["threshold"] (SFace cosine default 0.363).

## iris/liveness.py
    class LivenessChecker:
        def __init__(self, cfg)
        def check(self, gray, face_row) -> tuple[bool, str]
    IR-native spoof resistance: phone/monitor screens emit no 850nm IR and read near-black
    in the lit frames; require face-region variance >= min_variance and reject flat regions.

## iris/store.py  (root-only)
    class TemplateStore:
        def __init__(self, root="/var/lib/iris")
        def list_faces(self, user) -> list[dict]   # {"name","created","samples"} (NO embeddings)
        def add(self, user, name, embeddings: list[np.ndarray]) -> None
        def remove(self, user, name) -> bool
        def clear(self, user) -> None
        def embeddings_for(self, user) -> list[tuple[str, np.ndarray]]
    Storage: /var/lib/iris/<user>.enc — AES-256-GCM (cryptography AESGCM),
    12-byte random nonce prepended, plaintext = JSON. Key = /var/lib/iris/master.key (0600),
    32 random bytes, TPM-sealed via tpm2-tools when /dev/tpmrm0 exists (graceful fallback).
    NEVER store raw images. Only embeddings.

## iris/protocol.py
    SOCKET_PATH = "/run/irisd/socket"
    Newline-delimited UTF-8 JSON, one object per line, 64KiB max.
    send_request(req: dict, timeout: float, path=SOCKET_PATH) -> dict
    Ops (request -> response):
      {"op":"ping"}                        -> {"ok":true,"version":str}
      {"op":"auth","user":str,"timeout":f} -> {"ok":bool,"confidence":float,"reason":str,"face":str|null}
      {"op":"list","user":str}             -> {"ok":true,"faces":[...]}
      {"op":"cameras"}                     -> {"ok":true,"cameras":[...]}
      {"op":"enroll","user":str,"name":str}-> progress lines {"progress":f,"hint":str} then final {"ok":bool,...}
      {"op":"remove","user":str,"name":str}-> {"ok":bool}
      {"op":"clear","user":str}            -> {"ok":bool}
      {"op":"config_get"}                  -> {"ok":true,"config":{...}}
      {"op":"config_set","config":{...}}   -> {"ok":bool}
    `reason` vocabulary: match | no_match | no_face | timeout | camera_error |
                         not_enrolled | disabled | lockout | spoof_suspected

## SAFETY — non-negotiable
1. pam_iris.so MUST return PAM_AUTH_ERR/PAM_AUTHINFO_UNAVAIL on ANY error. Never PAM_SUCCESS.
2. It MUST be stacked `[success=done default=ignore]` so failure falls through to password.
3. It MUST enforce a hard wall-clock timeout and never block a login shell indefinitely.
4. Rate limit: max_failures within lockout_seconds -> reason "lockout".
5. Password auth must remain functional at every touched point.
