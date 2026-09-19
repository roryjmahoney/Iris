<!-- SPDX-License-Identifier: AGPL-3.0-only -->

# Configure Iris

Iris reads `/etc/iris/config.toml`, owned by root and readable by the desktop
settings app. A missing or malformed file does not stop password
authentication: known values are type-checked and bounded, rejected values fall
back to defaults, and diagnostics go to the `iris.config` logger.

Use the CLI for normal changes:

```bash
iris config
iris config keys
iris config get recognition.threshold
sudo iris config set recognition.threshold 0.363
sudo iris config unset recognition.threshold
```

Use the shipped threshold of `0.363` as the starting point. Higher values are
optional stricter cutoffs that can reject legitimate attempts; see the
[calibration guidance](CALIBRATION.md) before changing it.

Changes apply to the next authentication request. Restarting `irisd` forces an
immediate reload:

```bash
sudo systemctl restart irisd
```

## Core settings

### `[camera]`

| Key | Type | Default | Accepted range | Purpose |
|---|---|---:|---:|---|
| `device` | string | `/dev/video2` | — | V4L2 IR capture node; metadata nodes are refused |
| `width` | integer | `640` | 1–8192 | Requested capture width |
| `height` | integer | `360` | 1–8192 | Requested capture height |
| `ir_mode` | boolean | `true` | — | Request GREY capture and discard dark strobe frames |
| `min_frame_brightness` | float | `20.0` | 0–255 | Mean brightness below which an IR frame is discarded |

### `[recognition]`

| Key | Type | Default | Accepted range | Purpose |
|---|---|---:|---:|---|
| `threshold` | float | `0.363` | -1–1 | Minimum cosine similarity for a match |
| `detect_score` | float | `0.7` | 0–1 | Minimum YuNet detection confidence |
| `model_dir` | string | `/usr/share/iris/models` | — | Directory containing both ONNX files |
| `required_matches` | integer | `3` | 1–1000 | Consecutive matching, live frames required |
| `max_frames` | integer | `120` | 1–100000 | Frame budget for one attempt |

Root refuses to fall back from `model_dir` to a user-writable source checkout.
This prevents a privileged daemon from loading an ONNX graph that an
unprivileged user can replace.

### `[auth]`

| Key | Type | Default | Accepted range | Purpose |
|---|---|---:|---:|---|
| `enabled` | boolean | `true` | — | Master switch; false returns `disabled` without opening the camera |
| `timeout` | float | `8.0` | 0.5–60 | Wall-clock budget for an attempt |
| `max_failures` | integer | `5` | 1–64 | Failures allowed within the lockout window |
| `lockout_seconds` | integer | `60` | 0–86400 | Sliding window and lockout duration; zero disables lockout |

To suspend face authentication without editing PAM:

```bash
sudo iris config set auth.enabled false
```

### `[liveness]`

| Key | Type | Default | Accepted range | Purpose |
|---|---|---:|---:|---|
| `enabled` | boolean | `true` | — | Enable IR presentation-attack heuristics |
| `min_variance` | float | `12.0` | 0–65025 | Laplacian-variance floor for the face region |

Disabling liveness removes Iris's presentation-attack checks. Read
[the security model](SECURITY.md#3-presentation-attack-spoof-resistance) before
doing so.

## Advanced keys

The recognition and liveness implementations accept these additional scalar
keys. They are not in the declared CLI schema, so add them by hand. Unknown
scalar keys survive a settings-app rewrite, although comments and formatting do
not.

| Key | Default | Purpose |
|---|---:|---|
| `recognition.nms_threshold` | `0.3` | Non-maximum-suppression IoU threshold |
| `recognition.top_k` | `50` | Candidate boxes retained before NMS |
| `recognition.clahe_clip` | `2.0` | CLAHE clip limit |
| `recognition.clahe_grid` | `8` | CLAHE tile grid dimension |
| `liveness.min_face_brightness` | `25.0` | Absolute face-region brightness floor |
| `liveness.min_face_ratio` | `0.45` | Face brightness relative to the full frame |
| `liveness.max_saturated` | `0.20` | Maximum saturated fraction in the face region |
| `liveness.saturation_level` | `250` | Pixel value counted as saturated |
| `liveness.min_contrast` | `8.0` | Standard-deviation floor |
| `liveness.min_consecutive` | `2` | Consecutive liveness passes required |
| `liveness.min_region_px` | `24` | Minimum usable face-region side length |
| `liveness.history` | `16` | Per-attempt statistics history |

## Result reason codes

| Reason | Meaning | PAM result |
|---|---|---|
| `match` | An enrolled face matched | `PAM_SUCCESS` |
| `no_match` | A face was found but did not match | `PAM_AUTH_ERR` |
| `no_face` | No usable face was visible | `PAM_AUTH_ERR` |
| `timeout` | The attempt exceeded its budget | `PAM_AUTH_ERR` |
| `camera_error` | The configured camera could not be used | `PAM_AUTH_ERR` |
| `not_enrolled` | No usable enrolment is available | `PAM_AUTHINFO_UNAVAIL` |
| `disabled` | Face authentication is switched off | `PAM_AUTHINFO_UNAVAIL` |
| `lockout` | The per-user failure limit was reached | `PAM_AUTH_ERR` |
| `spoof_suspected` | IR liveness checks rejected the image | `PAM_AUTH_ERR` |

Specific liveness failures are intentionally collapsed to
`spoof_suspected` on authentication responses so they do not become an
attacker-tuning signal. Detailed reasons are available in debug logs and as
positioning guidance during enrolment.
