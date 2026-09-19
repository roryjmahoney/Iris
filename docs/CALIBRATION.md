<!-- SPDX-License-Identifier: AGPL-3.0-only -->

# Iris — Threshold Calibration (measured on target hardware)

These recorded measurements describe one subject on the original target machine.
They do not establish false-accept or false-reject rates across users or cameras.
Re-run `iris doctor --calibrate` after a camera or lighting change.

**Keep the shipped threshold of `0.363` as the starting point.** If recognition
is unreliable, add enrollment captures in different lighting and head positions
before considering a threshold change. A higher threshold can reject legitimate
attempts; these measurements do not quantify its security benefit.

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

Within this single session, the lowest same-person similarity was `0.621` and
p5 was `0.738`. Those observations describe frames captured seconds apart;
they do not establish a minimum score for later authentication attempts.
The post-enrolment measurements below include a legitimate attempt at `0.371`.

Iris ships `recognition.threshold = 0.363`, as specified in `SPEC.md` and
`iris.config.DEFAULTS`. Retain that default unless you deliberately choose to
experiment with a stricter cutoff and evaluate the resulting retries.

- `0.363` — shipped default; below all seven recorded post-enrolment scores.
- `0.500` — a stricter optional cutoff; above one of those seven scores.
- `0.600` — stricter still; also above that same recorded score. The small
  sample cannot predict how often future attempts would be rejected.

This study includes no impostor trials or presentation-attack evaluation.
Raising a cutoff reduces the set of accepted similarity scores, but does not
show that false accepts are halved, that look-alikes are rejected, or that
spoofing is prevented. Nor does this sample establish a universally safe
threshold for IR cameras. See the [security model](SECURITY.md).

The default `required_matches = 3` requires consecutive matching, live frames;
a single matching frame is insufficient. These measurements do not compare
its security contribution with threshold changes.

---

## Post-enrolment measurements

The study above compares frames captured *seconds apart in one session*, which is
an optimistic sample of matching conditions. The number that governs day-to-day behaviour is a live
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

Six of the seven recorded scores were between `0.728` and `0.872`; four runs
took `1.20 s`. The observed elapsed times ranged from `1.20 s` to `5.75 s`.
These runs are too few to establish general latency or reliability guarantees.

The cold-start run scored `0.371`, only `0.008` above the shipped threshold.
Its recorded score would not clear either `0.500` or `0.600`. This is evidence
against recommending a higher threshold on the assumption that genuine matches
always score above `0.621`.

## Practical guidance

Keep `0.363` as the starting point. For unreliable matching, check camera setup
and improve enrollment across lighting and head positions, then evaluate real
attempts against those saved templates. Better enrollment may help; it does not
guarantee first-attempt success.

If you deliberately raise the threshold, test across separate sessions,
including cold starts and your usual lighting and poses. Expect that additional
retries may occur, retain password fallback, and do not treat your own successful
matches as evidence of impostor or spoof rejection.
