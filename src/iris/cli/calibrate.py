"""``iris doctor --calibrate``: threshold calibration."""

from __future__ import annotations

import time
from typing import Any, Mapping

from iris import config as config_mod

from iris.cli.common import ProgressRenderer, render_table
from iris.cli.constants import EXIT_INTERRUPTED, EXIT_OK, PROG
from iris.cli.output import CommandError, console


def run_calibration(cfg: Mapping[str, Any], samples: int, timeout: float) -> int:
    """Measure detection latency and genuine-pair similarity on this hardware.

    This is the measurement behind ``docs/CALIBRATION.md``: it collects SFace
    embeddings of one live subject, then reports the *genuine* cosine
    distribution so an administrator can see how much headroom the configured
    threshold really has on their camera and lighting.

    It deliberately does not touch the configuration.  The impostor
    distribution is not measured here (that needs other people's faces), so the
    numbers bound false *rejects* only and the suggested threshold is advice,
    not a validated operating point.
    """
    try:
        from iris.camera import Camera, CameraError
        from iris.engine import EngineError, FaceEngine
        import numpy as np
    except ImportError as exc:
        raise CommandError(
            f"calibration needs OpenCV and numpy: {exc}",
            hint="sudo apt install python3-opencv python3-numpy",
        ) from exc

    console.print(f"{console.bold('Calibration')} — collecting {samples} samples")
    console.print(
        "Look at the camera and move your head slowly through the poses you "
        "would use to log in."
    )
    console.print()

    try:
        engine = FaceEngine(dict(cfg))
    except EngineError as exc:
        raise CommandError(str(exc), hint="run  iris doctor  to check the models") from exc

    detect_ms: list[float] = []
    embed_ms: list[float] = []
    scores: list[float] = []
    embeddings: list[Any] = []
    frames = 0
    progress = ProgressRenderer(console)

    try:
        with Camera.from_config(dict(cfg)) as cam:
            for gray in cam.frames(timeout):
                frames += 1
                start = time.perf_counter()
                faces = engine.detect(gray)
                detect_ms.append((time.perf_counter() - start) * 1000.0)

                face = FaceEngine.select_face(faces)
                if face is None:
                    progress.update(
                        len(embeddings) / samples,
                        f"no face visible ({len(embeddings)}/{samples})",
                    )
                    continue

                start = time.perf_counter()
                embeddings.append(engine.embed(gray, face))
                embed_ms.append((time.perf_counter() - start) * 1000.0)
                scores.append(float(face[14]))
                progress.update(
                    len(embeddings) / samples,
                    f"captured {len(embeddings)}/{samples}",
                )
                if len(embeddings) >= samples:
                    break
    except (CameraError, EngineError) as exc:
        progress.finish()
        raise CommandError(f"calibration failed: {exc}") from exc
    except KeyboardInterrupt:
        progress.finish()
        console.warn("calibration aborted")
        return EXIT_INTERRUPTED
    finally:
        progress.finish()

    if len(embeddings) < 2:
        raise CommandError(
            f"only {len(embeddings)} face(s) captured from {frames} lit frames",
            hint="sit 40-70cm from the screen, facing the camera, and try again",
        )

    similarities = [
        FaceEngine.compare(embeddings[i], embeddings[j])
        for i in range(len(embeddings))
        for j in range(i + 1, len(embeddings))
    ]
    array = np.asarray(similarities, dtype=np.float64)
    p5 = float(np.percentile(array, 5))
    median = float(np.percentile(array, 50))
    worst = float(array.min())
    best = float(array.max())

    detect = np.asarray(detect_ms, dtype=np.float64)
    threshold = float(cfg["recognition"]["threshold"])

    console.print()
    rows = [
        ["frames examined", f"{frames} lit"],
        ["faces embedded", f"{len(embeddings)}"],
        ["embedding dimension", f"{len(embeddings[0].ravel())}"],
        ["detection hit rate", f"{100.0 * len(embeddings) / max(frames, 1):.0f}%"],
        ["detection latency", f"mean {detect.mean():.1f} ms, p95 {float(np.percentile(detect, 95)):.1f} ms"],
        ["embedding latency", f"mean {float(np.mean(embed_ms)):.1f} ms"],
        ["best detection score", f"{max(scores):.3f}"],
        ["genuine cosine min", f"{worst:.3f}"],
        ["genuine cosine p5", f"{p5:.3f}"],
        ["genuine cosine median", f"{median:.3f}"],
        ["genuine cosine max", f"{best:.3f}"],
        ["configured threshold", f"{threshold:.3f}  (margin to worst pair {worst - threshold:+.3f})"],
    ]
    for line in render_table(["MEASUREMENT", "VALUE"], rows):
        console.print(line)

    # A tenth of a cosine below the worst genuine pair keeps a comfortable
    # false-reject margin; never suggest going below SFace's published
    # operating point, and never above 0.6, where the CALIBRATION notes show
    # retries start at awkward angles.
    suggestion = min(max(round(worst - 0.10, 2), config_mod.DEFAULTS["recognition"]["threshold"]), 0.60)
    console.print()
    if worst < threshold:
        console.warn(
            f"the worst genuine pair ({worst:.3f}) is below the configured "
            f"threshold ({threshold:.3f}); expect false rejections"
        )
    console.print(f"Suggested threshold for this hardware: {console.bold(f'{suggestion:.2f}')}")
    console.note(f"  sudo {PROG} config set recognition.threshold {suggestion:.2f}")
    console.note(
        "  This measures one subject, so it bounds false rejections only — it "
        "does not measure impostors."
    )
    return EXIT_OK
