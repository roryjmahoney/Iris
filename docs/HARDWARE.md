<!-- SPDX-License-Identifier: AGPL-3.0-only -->

# Camera and hardware notes

Iris is built around a V4L2 infrared capture node. Device numbers, formats,
brightness, and emitter behavior vary by laptop; discover them rather than
copying the original target's `/dev/video2` setting.

## Discover capture nodes

```bash
iris cameras
iris cameras --all
```

The first command shows usable capture nodes and marks the one Iris uses with
`*`. `--all` also shows metadata nodes. UVC metadata nodes can share the same
human-readable name as a camera but carry payload headers rather than images.
Iris reads the kernel's per-node `device_caps` and refuses
`V4L2_CAP_META_CAPTURE` nodes before OpenCV can wait indefinitely for frames.

By default `camera.device` is `auto`: Iris uses the first infrared capture node
each time it opens the camera, so a dock or kernel update that renumbers
`/dev/video*` does not break it. `auto` never falls back to a colour camera,
because a printed photograph defeats face authentication on one. To pin a
specific node instead:

```bash
sudo iris config set camera.device /dev/videoN
sudo iris test
```

Switch back with `sudo iris config set camera.device auto`.

## Validate IR strobing

```bash
iris doctor
```

`doctor` samples raw frames and reports whether it sees both illuminated and
dark frames:

- lit and dark frames: expected for a strobing emitter;
- all lit: usable, but brightness filtering has no effect;
- all dark: the emitter may not be firing or the configured brightness floor
  may be too high.

When `camera.ir_mode = true`, authentication discards frames below
`camera.min_frame_brightness`. The preview and diagnostics keep raw frames so
they can display and measure the complete stream.

## If your camera is not found or does not work

A common cause is an IR emitter that is off by default: `iris doctor` then
reports that every frame was dark. On many laptops the emitter has to be
enabled once with a tool such as
[linux-enable-ir-emitter](https://github.com/EmixamPP/linux-enable-ir-emitter).

Either way, please send a report. It is how support for new hardware gets
added:

```bash
iris hardware-report                 # Markdown, ready to paste
iris hardware-report --json          # the same data, machine-readable
iris hardware-report --no-capture    # skip opening the camera
```

It lists the machine model, OS and kernel, every video node with its USB
vendor:product id, driver, formats and frame sizes, and which node Iris would
use. It then samples that node for three seconds, recording **one brightness
number per frame** and no images, to tell whether the emitter strobes, stays
on, or never fires. It reads no serial numbers, user names or host names.
Paste the output into a
[hardware report](https://github.com/roryjmahoney/Iris/issues/new?template=hardware_report.yml).

## Compatibility

Results from hardware reports. Please add yours.

| Laptop | IR camera (USB id) | Node / format | Emitter | Status |
|---|---|---|---|---|
| LG (original development laptop) | LGE IR-FHD Camera | GREY 640×360 @ 15 fps | strobing | Works |

## Original target measurements

These values describe the machine used for the repository's calibration. They
are evidence for that device only:

| Node | Classification | Format |
|---|---|---|
| `/dev/video0` | RGB capture | MJPG, YUYV |
| `/dev/video1` | UVC metadata | no image format |
| `/dev/video2` | `LGE IR-FHD Camera` capture | GREY, 640×360 at 15 fps |
| `/dev/video3` | UVC metadata | no image format |

The IR emitter alternated between dark frames with mean brightness around 1.2
and illuminated frames around 55–62, leaving roughly 7.5 usable frames per
second. The default brightness floor of 20 sat between those populations.

The measured recognition pipeline was:

```text
illuminated frame
  → CLAHE (2.0, 8×8)
  → grayscale-to-BGR conversion
  → YuNet detection
  → SFace alignCrop and feature
  → 128-element float32 embedding
  → cosine similarity
```

Liveness checks use the raw frame, not its CLAHE-equalized copy. Equalization
would brighten a near-black replay surface and amplify sensor noise, weakening
the signals those checks inspect.

See [Calibration](CALIBRATION.md) for the complete target measurements. Re-run
`iris doctor --calibrate` after changing the camera, lighting, or physical
setup; do not treat one laptop's threshold study as portable certification.
