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

The first command shows usable capture nodes and marks the likely IR device.
`--all` also shows metadata nodes. UVC metadata nodes can share the same
human-readable name as a camera but carry payload headers rather than images.
Iris reads the kernel's per-node `device_caps` and refuses
`V4L2_CAP_META_CAPTURE` nodes before OpenCV can wait indefinitely for frames.

Set the selected node with:

```bash
sudo iris config set camera.device /dev/video2
sudo iris test
```

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
