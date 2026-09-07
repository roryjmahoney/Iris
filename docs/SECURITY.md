<!-- SPDX-License-Identifier: AGPL-3.0-only -->

# Iris — Security Model

This document describes what Iris defends against, what it does not, and how
the pieces that hold secrets are built. It is written for someone deciding
whether to enable face authentication on a real machine, and for someone
auditing the code.

Interface contract: [`../SPEC.md`](../SPEC.md). Threshold measurements:
[`CALIBRATION.md`](CALIBRATION.md).

---

## 1. Position in the authentication stack

**Face authentication is a convenience factor. It is weaker than the password
it sits in front of, and it is stacked so that it can only ever *shorten* a
successful login, never *replace* the password as the thing standing between an
attacker and the account.**

The single supported PAM stacking is:

```
auth    [success=done default=ignore]    pam_iris.so
auth    ...                              pam_unix.so
```

`default=ignore` is the safety interlock. Face recognition fails for entirely
innocent reasons constantly — the lid is shut, the room is dark, the user is
wearing a mask, the daemon is being upgraded, another application has the
camera, the IR emitter is in its dark strobe phase. With `ignore`, every one of
those outcomes is discarded from the stack's result and evaluation continues to
`pam_unix`, which prompts for a password exactly as it did before Iris was
installed.

Stacking the module `required` or `requisite` is a catastrophic
misconfiguration: a camera failure becomes an unrecoverable lockout of the
physical console with no password fallback. On a machine with full-disk
encryption and no second admin account that is unrecoverable without external
media. `iris doctor` fails the "PAM module" check on any control field other
than `[success=done default=ignore]`, and both the module's source header and
the `Makefile` refuse to be quiet about it.

The default `install.sh` run never modifies `/etc/pam.d`. PAM activation is an
explicit opt-in through `--gdm`, `--sudo`, or `--polkit`; each edit is atomic,
keeps the original password stack, and has a tested rollback path. `make
install` for the PAM module installs only the shared object and prints manual
stacking instructions.

---

## 2. Threat model

### Assets

| Asset | Where | Consequence if lost |
|---|---|---|
| Face templates (SFace embeddings) | `/var/lib/iris/<user>.enc` | Biometric linkage; **cannot be revoked**. Not invertible to a photograph. |
| Master AES key | TPM-sealed blob, or `/var/lib/iris/master.key` | Decrypts every template on the machine. |
| The authentication decision | `/run/irisd/socket` | Forging a `{"ok":true}` reply grants a login. |
| Configuration | `/etc/iris/config.toml` | Lowering `recognition.threshold` or disabling liveness weakens or defeats matching. |

### Adversaries considered

1. **Opportunistic impostor with your photograph.** Someone who has your face
   from social media and holds a phone or a print in front of the camera.
   *Defended* — see §3.
2. **Unprivileged local user on the same machine.** A logged-in account trying
   to read templates, drive enrolment, or forge an authentication.
   *Defended* — see §5, §6.
3. **Thief with the disk.** Someone who pulls the SSD and reads it elsewhere.
   *Defended where a TPM is usable* — see §4.
4. **Someone who resembles you.** A sibling, a twin.
   *Partially defended.* This is a threshold question, not a design question.
   Cosine similarity at 0.363 (or 0.5) separates unrelated faces well; it is
   not a guarantee against a close relative. See `CALIBRATION.md` for the
   measured genuine distribution and why the impostor side is *not* measured
   here.
5. **Determined attacker with fabrication capability.** Someone who builds an
   IR-reflective mask of your face.
   **NOT defended.** See §3.
6. **Root on the running machine.** Someone who is already root can read the
   unsealed key from `irisd`'s memory, rewrite the config, or replace the
   module.
   **Out of scope by construction** — root already owns the machine.
7. **Coercion.** Someone holding you in front of your own laptop.
   **NOT defended**, and not defensible by any face system without a duress
   signal.

### Explicitly out of scope

* Kernel, firmware, UEFI and TPM hardware compromise.
* Evil-maid attacks on an unencrypted boot chain.
* Physical camera replacement or a hardware implant on the USB/UVC path.
* Side channels: power analysis, timing of the recognition pipeline,
  electromagnetic emanations.
* An attacker who can already run code as root.

---

## 3. Presentation-attack (spoof) resistance

### The physical basis

The laptop's IR camera sees only the light its own 850nm emitter throws back at
it. A real face is a diffuse, three-dimensional, near-infrared-reflective
object: in a lit strobe frame it comes back bright and richly textured. A phone
or monitor replaying a photo or video emits visible light and essentially no
850nm energy, so the "face" on it returns almost nothing — the region reads
dark, flat and structureless. If the panel's glass catches the emitter instead,
it returns a blown-out specular hotspot. Both signatures are easy to separate
from a face without extra hardware.

### The tests

`src/iris/liveness.py` runs six checks per frame, on the **raw** frame, in this
order. (Recognition uses a CLAHE-equalised copy; liveness deliberately does
not, because CLAHE would rescale a near-black screen region up to mid-grey and
amplify its sensor noise into apparent texture, erasing both signals below.)

| # | Test | Rejects | Reason code |
|---|---|---|---|
| 1 | Frame mean >= `min_frame_brightness` | A dark strobe frame that bypassed filtering — every brightness measurement below would be meaningless. | `dark_frame` |
| 2 | Face-region mean >= `min_face_brightness` (25.0) **and** region/frame ratio >= `min_face_ratio` (0.45) | The core anti-replay test: a screen's "face" is dramatically darker than the genuinely illuminated scene around it. Checked both absolutely and relatively, because the absolute level depends on how close the user sits. | `no_ir_return` |
| 3 | Saturated fraction <= `max_saturated` (0.20) | Specular glare — the emitter mirrored off glass or a glossy print. Checked *before* the texture test, since a blown-out region carries high Laplacian variance at its clipped edges and would otherwise sail through. | `saturated` |
| 4 | Region std >= `min_contrast` (8.0) | Uniform surfaces (paper, a switched-off panel) that happen to clear the brightness floor. | `low_contrast` |
| 5 | Laplacian variance >= `min_variance` (12.0) | The discriminator between skin and any flat reproduction of it: real skin under IR shows pores, nostril and lip edges, eye sockets. | `flat_region` |
| 6 | Consecutive frames not byte-identical | A stuck V4L2 queue or frames injected from a file. Two independent 15fps captures of a real scene never produce identical statistics — sensor noise guarantees they differ. This does **not** detect a video replay; tests 2–5 cover that. | `static_input` |

Three geometric rejections (`no_face`, `face_too_small`, `out_of_frame`) mean
"the user is badly positioned", not "this is a spoof", and the daemon collapses
them to `no_face` — reporting `spoof_suspected` to someone sitting too far away
is both wrong and unhelpful.

The face region is inset 15% on each side before measurement. The detector's
box includes forehead, hair and background corners; hair is a poor IR reflector
and background sits at a different distance from the emitter, so both skew the
statistics. The inset keeps the measurement on skin — eyes, nose, mouth —
which is exactly what a spoof has to reproduce.

`min_face_ratio` is deliberately loose. Near-infrared reflectance of skin
varies far less across skin tones than visible-light reflectance does, but it is
not identical, and a false rejection here is a fairness problem, so the bar sits
well below any plausible live face.

### Multi-frame confirmation

A single frame can pass by luck — motion blur, a passing reflection. The daemon
requires `recognition.required_matches` (default 3) **consecutive** frames that
all clear the recognition threshold *and* the liveness checks, and
`LivenessChecker` separately tracks a passing streak (`min_consecutive`, default
2). Any failure resets the streak, so an attacker cannot alternate a spoof frame
with a real one to accumulate confidence. At 7.5 usable fps, three consecutive
matches costs roughly 0.4 s.

### What is NOT defended against

Be clear-eyed: this is a filter, not a guarantee.

* **No certification.** Nothing here has been tested against ISO/IEC 30107-3.
  There is no measured APCER/BPCER for this implementation.
* **A high-quality IR-reflective mask defeats it.** A resin or silicone mask
  with skin-like NIR reflectance and real three-dimensional relief produces
  bright, textured, varying IR returns. These tests cannot tell it from a face.
  Neither can they stop a photograph printed on IR-reflective substrate and
  shaped to the face's contours.
* **No depth check is possible.** This hardware has a single IR sensor and no
  structured-light or time-of-flight projector, so there is no depth map to
  reason about. The strongest anti-spoof signal available to Face ID-class
  systems is simply not present.
* **No challenge-response.** No blink or head-turn prompt, so a video replay on
  a hypothetical IR-emitting display would pass the texture tests.
* **No coercion resistance.** A real face held in front of the camera under
  duress is, by every measure here, live.
* **`liveness.enabled = false` removes all of it.** That switch exists for
  diagnosis on hardware where the heuristics misfire. Setting it on a machine
  you care about reduces Iris to "does this photograph resemble the enrolled
  face".

### Information disclosure to attackers

Which specific liveness test failed is never sent over the wire during
authentication — the protocol collapses every rejection into the single reason
`spoof_suspected`. Telling an attacker which test they tripped hands them a
tuning signal. The specific code is logged locally at debug level.

Enrolment *does* return human-readable positioning hints, because there is no
attacker to help at enrolment time and a user who cannot get a template
recorded will simply give up.

---

## 4. Template storage, encryption and TPM sealing

`src/iris/store.py` is the only module in Iris that touches persistent
biometric material.

### What is stored

**Only SFace embeddings — 128 float32 values per sample.** Never raw images,
never crops, never landmarks-plus-pixels. A stolen template file cannot be
turned back into a photograph of the user's face. `list_faces()` returns
`{name, created, samples}` and structurally cannot return embeddings.

### On-disk format

```
/var/lib/iris/<user>.enc          root:root 0600
/var/lib/iris/                    root:root 0700

[ 12-byte random nonce ][ AES-256-GCM ciphertext || 16-byte tag ]

AAD       = the username, as UTF-8
plaintext = {"version": 1,
             "faces": [{"name": str, "created": iso8601,
                        "embeddings": [[float, ...]]}]}
```

Design points and why:

* **AES-256-GCM** via `cryptography`'s `AESGCM`. Authenticated encryption, so
  tampering is detected rather than silently decrypted into garbage.
* **The username is the AAD.** An attacker with write access to the directory
  cannot rename `mallory.enc` to `root.enc` and log in as root — the AEAD tag
  will not verify against the new AAD and decryption fails closed.
* **A fresh random 12-byte nonce per write.** Nonce reuse under the same key
  leaks the XOR of plaintexts and the GCM authentication subkey.
* **A failed tag is an error, never "no templates enrolled".** Silently
  treating tampering as an empty store would hide an attack. The daemon maps
  it to `not_enrolled` over the wire (the remedy — re-enrol — is the same) and
  logs the real cause loudly.
* **All writes are atomic**: temp file in the same directory, `fsync`,
  `os.replace`, then `fsync` on the directory so the rename itself is durable.
  A power cut mid-enrolment leaves the previous templates intact rather than a
  truncated file that would lock the user out. `os.replace` also never follows
  a pre-existing symlink at the destination.
* **Symlinks are refused**, for both `/var/lib/iris` itself and the individual
  template files. If an attacker can plant `/var/lib/iris -> /home/mallory`,
  every subsequent `chmod`/`chown` would land on their tree.
* **Size bounds**: at most 16 faces per user, 64 embeddings per face, 1024
  dimensions per embedding, 8 MiB per file, 64 characters per label. These stop
  a buggy or malicious caller turning the template file into an unbounded blob
  that then has to be decrypted in RAM.
* **Usernames are validated** against `^[A-Za-z0-9_][A-Za-z0-9_.-]{0,31}\$?$`
  before becoming a path component — deliberately stricter than `useradd`'s
  `NAME_REGEX`. This check exists in **both** `iris.store` and `iris.daemon`;
  the duplication is intentional defence in depth.
* **Every mutating operation asserts `euid == 0`** rather than producing a
  half-written, wrongly-owned tree.

### The master key

`/var/lib/iris/master.key` — 32 random bytes from `os.urandom`, mode 0600,
owner root:root. Resolution order in `KeyManager.load_key()`:

1. Sealed blob present and TPM usable -> unseal it.
2. Sealed blob present but TPM unusable -> the plain key file if one exists,
   otherwise **`KeyUnavailableError`**. It never mints a replacement: a new key
   would render every existing template permanently undecryptable, which looks
   to the user like "face login mysteriously forgot me" and silently destroys
   data.
3. Plain key file present -> use it.
4. Nothing present -> generate 32 random bytes, try to seal them, and write the
   plain key file **only** if sealing or its verification failed.

If the key file is found with the wrong mode or owner, it is repaired (with a
loud warning) rather than refused. Wrong permissions mean the key may have
leaked, but refusing would lock the user out of their own machine; the operator
decides whether to rotate.

### TPM sealing

Sealing is attempted whenever `/dev/tpmrm0` exists and the `tpm2-tools` binaries
are present. Any failure degrades cleanly to the plain 0600 key file rather than
bricking authentication.

* **`/dev/tpmrm0`, never `/dev/tpm0`.** The raw device is single-open and
  grabbing it fights `tpm2-abrmd` and the in-kernel resource manager that
  everything else on the system uses.
* **Owner-hierarchy ECC primary, SHA-256**, persisted at handle `0x81010F00`.
  Persisting the *primary* (not the sealed object) means unsealing costs one
  `TPM2_Load` instead of a ~1 s `TPM2_CreatePrimary` on every login. The sealed
  blob itself stays on disk as `master.key.tpm.pub` / `master.key.tpm.priv`.
  `0x81010F00` avoids handles commonly squatted by `systemd-cryptenroll`
  (`0x81000001`) and `clevis` (`0x81000000`). If the handle is already taken,
  Iris falls back to re-deriving the primary each time — slower, still correct.
* **No PCR policy is attached, on purpose.** The goal is "these templates only
  decrypt on this machine", not "only in this boot state". Binding to PCRs would
  break face login on every kernel and firmware update, which in practice gets
  the whole feature disabled by the user — a worse outcome than the marginal
  gain. This means a sealed key **is** recoverable by an attacker who boots the
  machine into an OS they control on the same hardware; sealing defends against
  disk theft, not against physical possession of the whole machine.
* **The round trip is verified before the plaintext is discarded.** A sealed
  blob that cannot be unsealed is indistinguishable from data loss, so
  `_create_key()` seals, unseals, and compares — and only then returns without
  writing a plain key file.
* **The plain key file is shredded once a sealed blob exists.** Keeping both
  would defeat the entire point of sealing.
* Transient plaintext during sealing lives in a temp directory **inside**
  `/var/lib/iris` (0700 root), not `/tmp`, so it is unreadable by other users
  for the microseconds it exists regardless of how `/tmp` is mounted, and it is
  overwritten before being unlinked.

Check the state at any time with `sudo iris status` ("key storage") or
`iris doctor` (the "TPM" check).

### Logging

**Nothing biometric is ever logged.** Embeddings and frames stay in memory.
Logs carry counters, reason codes and, at debug level, similarity scores — none
of which are invertible to an image.

---

## 5. Socket and privilege model

### The boundary

```
/run/irisd/         root:root 0700
/run/irisd/socket   root:root 0600    SOCK_STREAM, AF_UNIX
```

Exactly one process — `irisd`, running as root — opens the IR camera, loads the
ONNX models, and reads or writes `/var/lib/iris`. Everything else is a client.

The daemon refuses to start with a non-zero euid. It creates and repairs its
own runtime directory, tightening the mode and re-owning it if systemd or
anything else left it `0755`, and refuses outright if the path is a symlink —
a symlink there would let whoever planted it redirect the socket, and the
daemon's own `chmod`/`chown`, into a directory they control.

### Authorisation

Two independent controls:

1. **Filesystem permissions.** The `0700` directory alone keeps unprivileged
   users out; they cannot even `stat` the socket.
2. **`SO_PEERCRED` on every connection.** The peer's uid must be 0 or the
   connection is refused with an explanatory error. The kernel fills this in at
   `connect()` time and the client cannot forge it, which makes it a real
   authorisation check rather than a hint.

The second exists because the first is a packaging property. A future unit-file
change that loosened the socket mode must not silently open a root daemon to
every local user.

### Resource bounds

| Bound | Value | Why |
|---|---|---|
| Max message size | 64 KiB including the newline | A root daemon accepting connections from local clients must not let one stream bytes forever. Enforced *while reading*, aborting as soon as the limit is crossed rather than after buffering. |
| Max concurrent connections | 16 | Rejects a fork-bomb-style client early. |
| Listen backlog | 8 | More than a couple of clients waiting means something is wrong. |
| Idle timeout | 30 s | A connection that sits idle between requests is closed. |
| Shutdown grace | 10 s | In-flight requests get this long; a client whose request is cut short sees the connection close, which every caller already treats as a failure. |
| Camera lock | one at a time | `/dev/video2` is single-open and the emitter strobes; two concurrent captures produce read errors for both. Every capture in the system funnels through one lock and concurrent requests queue. |

Every socket operation in `iris.protocol` is bounded by a deadline. The helpers
**refuse** to operate on a socket whose timeout is `None`: a blocking read with
no deadline inside the PAM path would hang a login shell, which SPEC SAFETY rule
3 forbids outright.

### Input validation

* The op must be a string in the closed set of nine operations.
* Usernames are re-validated in the daemon before reaching anything that builds
  a path from them.
* Face labels are capped at 64 characters and must contain no control
  characters.
* `config_set` type-checks every incoming value against the schema and rejects
  mismatches rather than letting `iris.config` silently fall back to a default —
  over the wire that would look like a successful write that did nothing. A
  proposed `camera.device` is checked for being a real, non-metadata capture
  node before it is written.
* A client may shorten or lengthen `auth.timeout` for one attempt but is
  clamped to `[0.5, 60]`, so a client-supplied `timeout: 1e9` cannot hold a
  login open.

### Failure philosophy

Every operation fails closed. An unexpected exception anywhere in a request
handler becomes `{"ok": false}` with a reason from the closed vocabulary, never
a partial success and never an unhandled traceback that drops the connection
while PAM is waiting on it. An `auth` failure always carries a valid reason code
even when the cause was a malformed request, because PAM switches on it.

### The unprivileged GUI

The GTK4 app is unprivileged and cannot reach the socket. Every operation that
touches root-owned state goes through `pkexec iris <subcommand>`: polkit prompts
once, the CLI runs as root, and the GUI parses its output. Reading
`/etc/iris/config.toml` is the one exception — it is `0644` precisely so the
settings panel can show current values without a password prompt.

Settings are staged and applied in a single privileged call rather than
instant-apply, specifically to avoid raising a polkit prompt per switch flipped.
Training people to type their password into repeated dialogs without reading
them is itself a security failure.

### Model loading

`FaceEngine` resolves `recognition.model_dir` and, for a **non-root** process,
falls back to the source checkout's `models/` directory if the configured one is
incomplete — convenient for development. **Root gets the configured path or a
hard failure.** The daemon runs as root and a checkout lives in a user-writable
home directory; allowing the fallback there would mean a broken or partial
install silently causes a root process to load an ONNX graph an unprivileged
user can rewrite. That is both a parser attack surface and a way to swap in a
recogniser that matches any face.

---

## 6. The PAM module

`pam/pam_iris.c` runs inside every `sudo`, every `su`, every graphical unlock
and every login on the machine. A bug there does not produce a wrong answer on
a web page; it locks the owner out of their computer, or lets someone else in.
It is therefore written to a deliberately narrow standard.

### It does almost nothing

No image processing. No secrets. No device nodes. No linked libraries beyond
`libpam` itself. It is a client for one UNIX-socket request:

```
-> {"op":"auth","user":"alice","timeout":7.000}\n
<- {"ok":true,"confidence":0.81,"reason":"match","face":"default"}\n
```

### Exactly one success path

`rv` is initialised to `PAM_AUTH_ERR` and is assigned `PAM_SUCCESS` from a
single branch: the daemon answered, in a well-formed framed message, with a
top-level `"ok": true`. Daemon missing, socket refused, permission denied, short
write, truncated reply, oversized reply, unparseable JSON, missing `ok` key,
deadline expired, internal error, unknown user, control characters in the
username — every one returns a non-success code. The invariant is re-checked
immediately before returning, so that a future edit which somehow let `rv` reach
`PAM_SUCCESS` without an affirmative verdict converts it back into a denial
rather than into a security incident.

The two failure codes are distinguished on purpose:

* **`PAM_AUTHINFO_UNAVAIL`** — "I could not even ask the question." Daemon not
  running, socket absent, connect refused or denied, face auth disabled, or
  nothing enrolled. This tells the rest of the stack, `pam_faillock`, and the
  admin reading syslog that this is not an authentication *failure* by the user;
  nothing was attempted. It must not count against their failed-attempt budget.
* **`PAM_AUTH_ERR`** — "I asked, and the answer was no." No match, no face,
  timeout, camera error, spoof suspected, lockout, or any malformed reply.

### It cannot hang a login

A PAM module that blocks forever is worse than one that always denies: an
unkillable `sudo` or a frozen greeter is an outage. Three independent
mechanisms bound the runtime:

1. A wall-clock deadline on **`CLOCK_MONOTONIC`**, taken once and re-checked
   before *every* syscall — monotonic, not realtime, so an NTP step or a
   suspend/resume cannot move the finish line.
2. `SO_RCVTIMEO` / `SO_SNDTIMEO`, re-armed from the remaining budget before
   every `send()` and `recv()`, so a peer that accepts the connection and then
   goes silent — or dribbles one byte per second — still cannot outlast the
   deadline.
3. A **non-blocking `connect()` driven by `poll()`**, because `connect()` on a
   UNIX socket whose listen backlog is full blocks and is *not* affected by
   `SO_SNDTIMEO`.

The deadline starts *after* `pam_get_user()` returns, so a user typing their
name slowly does not eat the camera's budget. Default budget 8 s, clamped to
`[1, 60]` via the `timeout=` module argument.

### Memory and parsing safety

* **No heap allocation at all.** Every buffer is a bounded automatic array, so
  there is no allocation failure path and nothing to leak or double-free.
* No `strcpy`/`strcat`/`sprintf`/`gets` anywhere. Only `snprintf` with checked
  return values, `memcpy` with pre-verified bounds, and explicit length
  tracking.
* The JSON reply is parsed by a hand-rolled scanner that never writes, never
  reads outside `[buf, buf+len)`, understands string quoting so a `"ok":true`
  appearing *inside* a string value cannot be mistaken for the real key, and
  refuses anything it does not fully understand. Reply buffer is a fixed 8 KiB;
  at most a handful of framed lines are read.
* **The reply is never logged verbatim.** Only a length-capped,
  character-class-sanitised copy of the `reason` token reaches syslog, so a
  malformed or hostile reply cannot inject newlines or control sequences into
  the system log.
* The user-facing message is chosen from a fixed table *in the module*, keyed by
  reason code. The daemon's own strings are never echoed to a terminal, so a
  malformed reply cannot print arbitrary text into a login prompt.

`make check` compiles with `-Wpedantic -Wshadow -Wconversion -Wsign-conversion
-Wcast-qual -Wformat=2 -Wstrict-prototypes -Wmissing-prototypes -Wvla` and is
expected to be clean.

---

## 7. Rate limiting

SPEC SAFETY rule 4: `max_failures` within `lockout_seconds` yields the reason
`lockout`.

`FailureTracker` in `src/iris/store.py` is a sliding-window counter:

* **Defaults**: 5 failures inside 60 seconds. `lockout_seconds = 0` disables the
  lockout entirely; `max_failures <= 0` disables face auth (always locked). Both
  are honoured rather than clamped so the config can express either policy.
* **Timestamps come from `time.monotonic()`**, so NTP steps, suspend/resume and
  DST cannot shorten — or extend forever — a lockout.
* **Deliberately not persisted.** A reboot clearing the lockout is acceptable:
  an attacker who can reboot the machine has better options. A persistent file
  would be one more root-owned thing to get wrong, and a lockout that survives
  reboots is an excellent way to brick a laptop.
* **Bounded memory.** An LRU capped at 1024 users, with a per-user history cap
  of 64 entries and pruning on read, so an attacker spraying random usernames at
  the socket cannot grow it without bound.
* A successful authentication, or a fresh enrolment, resets the user's history.
* Only genuine authentication failures count. Administrative conditions —
  `disabled`, `not_enrolled` — do not.
* The lockout response carries `retry_after` so the CLI and GUI can say
  something useful.

**An Iris lockout never affects password authentication.** It stops face
attempts for that user for the window; `pam_unix` is untouched.

Note what this does and does not buy. It stops a naive automated replay loop
against the camera. It does not stop a patient attacker who waits 60 seconds
between attempts — for a biometric with no rate-limitable secret to guess, the
matching threshold, not the counter, is what bounds the false-accept risk.

---

## 8. Configuration as an attack surface

`/etc/iris/config.toml` is `root:root 0644` — world-readable, root-writable. It
contains no secrets. An attacker who can write it can:

* lower `recognition.threshold` toward "accept any face" (the loader logs a loud
  warning below 0.20 on every read),
* set `liveness.enabled = false`,
* point `recognition.model_dir` at a directory of their own models,
* point `camera.device` at a node they control.

All of those require root, at which point they own the machine anyway. The
mitigations that exist are about *accidents*, not attackers:

* `load_config()` never raises. A malformed file yields defaults plus a log line
  rather than a machine nobody can log into.
* Wrong-typed values fall back to their default; right-typed but out-of-range
  values are clamped, with the change logged.
* `auth.timeout` is clamped to a 60 s ceiling as a safety requirement, so no
  edit can make PAM hold a login shell open indefinitely.
* `iris config set` refuses out-of-range values rather than silently clamping
  them, because someone typing a value at a prompt should be told it is wrong.
* `bool` is checked before `int` throughout (in Python `bool` subclasses `int`),
  so `width = true` is rejected rather than accepted as 1.

---

## 9. Reporting a vulnerability

**Please do not open a public issue for anything that lets an unauthorised
person authenticate, read templates, or recover the master key.** Report it
privately to the maintainer of this repository first, so a fix can ship before
the technique is public.

There is no published security contact or PGP key in this tree yet. Until there
is, contact the repository owner directly through whatever private channel you
already have.

A useful report includes:

* the component (`pam_iris.c`, `daemon.py`, `store.py`, …) and the version
  (`iris --version`),
* what an attacker gains and what access they need to start,
* reproduction steps, ideally against a scratch install,
* for a spoof: what the artefact was made of, roughly what it cost, and how many
  attempts out of how many succeeded. "A printed photo worked once" and "a
  printed photo works every time" are different findings.

Things that are known and documented are not vulnerabilities: see §3 "What is
NOT defended against". A working IR-reflective mask attack is already
anticipated; a *cheap and reliable* one is still worth reporting, because
"cheap" changes the threat model even when "possible" does not.

If you find a way to make Iris **deny** a legitimate login that would otherwise
have succeeded via password — a hang, a crash that takes down the stack, a PAM
return code that stops the fallback — treat that as equally serious. Locking the
owner out of their own machine is the failure this whole design is arranged to
prevent.
