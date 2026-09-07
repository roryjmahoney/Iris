<!-- SPDX-License-Identifier: AGPL-3.0-only -->

# Iris — Threshold Calibration (measured on target hardware)

Measured directly on the deployment machine, not assumed. Re-run with `iris doctor --calibrate`
after any camera or lighting change.

## Rig
- Camera: `/dev/video2` — LGE IR-FHD, GREY 8-bit, 640x360 @ 15 fps
- Pipeline: illuminated-frame selection -> CLAHE(2.0, 8x8) -> GRAY2BGR -> YuNet -> SFace alignCrop+feature
- Samples: 40 accepted frames, single subject, ambient office lighting

## Results

| Metric | Value |
|---|---|
| Embedding dimension | **128** (float32) |
| Detection latency (mean) | **14.9 ms** |
| Detection latency (p95) | **17.8 ms** |
| Detection hit rate on lit frames | 100% (43/43 in a prior run) |
| YuNet best detection score | 0.904 |

### Same-person cosine similarity, IR grayscale

| Statistic | Cosine |
|---|---|
| min | 0.621 |
| p5 | 0.738 |
| median | 0.857 |
| max | 0.978 |

## Interpretation

SFace's published `0.363` cosine threshold is tuned on **RGB** LFW imagery. The concern was that
grayscale IR input would depress genuine-pair similarity toward the threshold. Measurement shows the
opposite: the genuine distribution sits far above it, with a **+0.375 margin at p5** and a worst
observed genuine pair still at 0.621.

**Consequence:** 0.363 is unnecessarily permissive on this hardware. Every genuine sample cleared
0.62, so the threshold can be raised substantially to tighten impostor rejection without introducing
false rejections.

- `0.363` — upstream default. Safe, but leaves ~0.26 of unnecessary slack below the worst genuine pair.
- **`0.500` — recommended default.** Still 0.12 below the worst observed genuine sample and 0.24
  below p5, while cutting the accepted impostor region roughly in half.
- `0.600` — hardened. Approaches the observed genuine minimum; expect occasional retries at bad angles.

**Iris nonetheless ships `threshold = 0.363`,** the value `SPEC.md` specifies and
`iris.config.DEFAULTS` contains — 0.5 is a *recommendation*, surfaced as such in the settings panel,
not the shipped default. The reason is the limit of this study: it is a *single-subject*
genuine-pair measurement. It bounds the false-reject side well, but it does **not** measure the
impostor distribution, which is what the upstream 0.363 figure was derived from. Raising the shipped
default on that evidence would trade a well-studied operating point for one validated against a
sample of one. Raise it yourself with `sudo iris config set recognition.threshold 0.5` once you have
lived with face unlock for a few days and know your own genuine-pair floor.

Consecutive-match confirmation (`required_matches`) matters more than threshold tuning for
robustness: at 15 fps with ~50% of frames dark, 3 consecutive matches costs roughly 0.4 s and makes a
single lucky frame insufficient to authenticate.


---

## Post-enrolment measurement (the one that actually matters)

The study above compares frames captured *seconds apart in one session*, which is
an optimistic upper bound. The number that governs day-to-day behaviour is a live
frame compared against **templates enrolled in an earlier session** — different
pose, different lighting, different hair. Measured after real enrolment
(15 samples, `primary`), seven consecutive `iris test` runs:

| Run | Confidence | Elapsed |
|---|---|---|
| 1 (cold start) | 0.371 | 1.59 s |
| 2 | 0.872 | 5.75 s |
| 3 | 0.728 | 1.20 s |
| 4 | 0.730 | 1.20 s |
| 5 | 0.774 | 1.20 s |
| 6 | 0.857 | 1.20 s |
| 7 | 0.845 | 1.87 s |

Steady state is **0.73-0.87**, which corroborates the within-session p5 of 0.738.
Authentication settles at **~1.2 s**; the first attempt after the camera has been
idle costs a few seconds of warm-up.

**The outlier is the interesting part.** Run 1 scored 0.371 — over the shipped
0.363 threshold by 0.008. A marginal frame at an awkward angle really does occur.
That single data point is the argument *against* raising the threshold to 0.5 as
casually as the section above suggests: 0.5 would have rejected that attempt and
forced a retry. Six of seven runs would still pass comfortably.

So the shipped default of **0.363 stands**, and 0.5 remains a deliberate
hardening choice with a known cost (an occasional retry at bad angles), not a
free upgrade. If you want the margin without the retries, the better lever is a
richer enrolment - add captures in different lighting and head positions - rather
than moving the threshold.
