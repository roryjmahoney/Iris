<!-- SPDX-License-Identifier: AGPL-3.0-only -->

# Third-party notices

The Iris license grant in [`LICENSE`](LICENSE) applies to Iris source code and
documentation. It does not relicense the binary model artifacts listed below.
Those files remain subject to their original upstream license terms.

## Bundled ONNX model artifacts

Both binaries match the Git LFS object IDs published by OpenCV Zoo at commit
[`47534e27`](https://github.com/opencv/opencv_zoo/commit/47534e27c9851bb1128ccc0102f1145e27f23f98).
The model directories carry their own licenses, which take precedence over the
repository-level OpenCV Zoo license.

| File | Role | Upstream artifact | License | SHA-256 / Git LFS object ID |
|---|---|---|---|---|
| `models/face_detection_yunet_2023mar.onnx` | YuNet face detection | [OpenCV Zoo](https://github.com/opencv/opencv_zoo/blob/47534e27c9851bb1128ccc0102f1145e27f23f98/models/face_detection_yunet/face_detection_yunet_2023mar.onnx) | [MIT](LICENSES/MIT.txt) | `8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4` |
| `models/face_recognition_sface_2021dec.onnx` | SFace face recognition | [OpenCV Zoo](https://github.com/opencv/opencv_zoo/blob/47534e27c9851bb1128ccc0102f1145e27f23f98/models/face_recognition_sface/face_recognition_sface_2021dec.onnx) | [Apache-2.0](LICENSES/Apache-2.0.txt) | `0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79` |

The bundled MIT text preserves the YuNet notice:
Copyright (c) 2020 Shiqi Yu `<shiqi.yu@gmail.com>`. The SFace model directory
contains the Apache License 2.0 and no separate `NOTICE` file at the pinned
revision. See the upstream [YuNet](https://github.com/opencv/opencv_zoo/tree/47534e27c9851bb1128ccc0102f1145e27f23f98/models/face_detection_yunet)
and [SFace](https://github.com/opencv/opencv_zoo/tree/47534e27c9851bb1128ccc0102f1145e27f23f98/models/face_recognition_sface)
model cards for authorship, references, and citations.

## System dependencies

Ubuntu packages used by Iris—GTK, libadwaita, PyGObject, OpenCV, NumPy,
cryptography, PAM, systemd, polkit, and TPM tools—are installed from the
operating system and are not vendored in this repository. Their own package
licenses continue to apply.
