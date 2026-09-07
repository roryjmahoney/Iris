/*
 * ============================================================================
 *  pam_iris.c — PAM authentication module for Iris (IR face authentication)
 * ============================================================================
 *
 *  WHAT THIS MODULE DOES
 *  ---------------------
 *  It asks `irisd` — which owns the IR camera, the enrolled templates and the
 *  rate limiter — whether the named user is in front of the camera right now.
 *  It performs NO image processing, holds NO secrets and opens NO device
 *  nodes.  It is a ~700-line, dependency-light client for one UNIX-socket
 *  request:
 *
 *      -> {"op":"auth","user":"alice","timeout":7.000}\n
 *      <- {"ok":true,"confidence":0.81,"reason":"match","face":"default"}\n
 *
 *  All of the difficulty in this file is in the *failure* paths, because this
 *  code runs inside every `sudo`, every `su`, every graphical unlock and every
 *  login on the machine.  A bug here does not produce a wrong answer on a web
 *  page; it locks the owner out of their computer, or lets someone else in.
 *
 *
 *  FAIL-CLOSED DESIGN (SPEC.md, SAFETY rule 1)
 *  -------------------------------------------
 *  There is exactly ONE code path in this file that returns PAM_SUCCESS: the
 *  daemon answered, in a well-formed framed message, with a top-level
 *  `"ok": true`.  Every other outcome — daemon missing, socket refused,
 *  permission denied, short write, truncated reply, oversized reply,
 *  unparseable JSON, missing "ok" key, deadline expired, malloc-free internal
 *  error, unknown user, control characters in the username — returns a
 *  non-success code.  `rv` is initialised to PAM_AUTH_ERR and is only ever
 *  assigned PAM_SUCCESS from the single `authenticated == true` branch, which
 *  is re-checked once more immediately before returning.
 *
 *  The distinction between the two failure codes is deliberate:
 *
 *      PAM_AUTHINFO_UNAVAIL — "I could not even ask the question."
 *          Daemon not running, socket absent, connect refused/denied, face
 *          auth disabled in config, or the user has nothing enrolled.  This
 *          tells the rest of the stack (and pam_faillock, and the admin
 *          reading syslog) that this is not an authentication *failure* by
 *          the user; nothing was attempted.  It must not count against the
 *          user's failed-attempt budget.
 *
 *      PAM_AUTH_ERR — "I asked, and the answer was no."
 *          No match, no face, timeout, camera error, spoof suspected,
 *          lockout, or any malformed/unsafe reply.
 *
 *
 *  WHY THE STACKING MUST BE  [success=done default=ignore]
 *  -------------------------------------------------------
 *  In every file under /etc/pam.d, this module MUST be listed as:
 *
 *      auth    [success=done default=ignore]    pam_iris.so
 *      auth    required                         pam_unix.so   # (or @include)
 *
 *  and it MUST appear BEFORE the password module, never as the only auth
 *  module in a stack.  The reason is the two halves of that control value:
 *
 *    * `default=ignore` is the safety interlock.  Face recognition fails for
 *      entirely innocent reasons all the time: the lid is shut, the user is
 *      wearing a mask, the room is dark, the daemon is being upgraded, the
 *      camera is claimed by a video call, the IR emitter is strobing dark.
 *      With `ignore`, every one of those outcomes is discarded from the
 *      stack's result and evaluation continues to pam_unix, which prompts for
 *      a password exactly as it did before Iris was installed.  This is what
 *      makes SAFETY rule 5 ("password auth must remain functional at every
 *      touched point") true in practice.
 *
 *      Marking this module `required` or `requisite` would be a catastrophic
 *      misconfiguration: a camera failure would become an unrecoverable
 *      lockout of the physical console, with no password fallback, and on a
 *      machine with full-disk encryption and no other admin account that is
 *      unrecoverable without external media.  `sufficient` is nearly
 *      equivalent to the value above and is tolerable, but the explicit form
 *      is preferred because it states the intent unambiguously and does not
 *      also inherit `new_authtok_reqd=done`.
 *
 *    * `success=done` ends the auth stack immediately on a face match so that
 *      the user is not asked for a password as well.  `done` (rather than a
 *      numeric jump) keeps the stack correct even when other modules are
 *      added or removed around it later.
 *
 *  Nothing in this file can enforce its own stacking — that is the installer's
 *  job — but everything in this file is written on the assumption that a
 *  non-success return is *harmless* and will be ignored.  That assumption is
 *  what lets it be aggressively paranoid: when in doubt, fail.
 *
 *
 *  WHY IT CANNOT HANG A LOGIN (SPEC.md, SAFETY rule 3)
 *  ---------------------------------------------------
 *  A PAM module that blocks forever is worse than one that always denies: an
 *  unkillable `sudo` or a frozen greeter is an outage.  Three independent
 *  mechanisms bound this module's runtime:
 *
 *    1. A wall-clock deadline on CLOCK_MONOTONIC, taken once and re-checked
 *       before *every* syscall.  CLOCK_MONOTONIC (not CLOCK_REALTIME) so that
 *       an NTP step or a suspend/resume cannot move the finish line.
 *    2. SO_RCVTIMEO / SO_SNDTIMEO, re-armed from the remaining budget before
 *       every send() and recv(), so a peer that accepts the connection and
 *       then goes silent — or dribbles one byte per second — still cannot
 *       outlast the deadline.
 *    3. A non-blocking connect() driven by poll(), because connect() on a
 *       UNIX socket whose listen backlog is full blocks and is NOT affected
 *       by SO_SNDTIMEO.
 *
 *  Everything else is bounded too: the reply buffer is a fixed 8 KiB, at most
 *  a handful of framed lines are read, and every loop has a termination
 *  condition that does not depend on the peer behaving.
 *
 *  Two things sit outside this module's control, both because they hand
 *  control to the application's conversation function:
 *
 *    - pam_get_user(), which may prompt for a username.  The deadline is
 *      therefore started *after* it returns, so a user typing their name
 *      slowly does not eat the camera's budget.
 *    - pam_prompt(PAM_TEXT_INFO, ...), the "look at the camera" notice.
 *
 *  Neither is bounded by our CLOCK_MONOTONIC deadline, so a hung conversation
 *  function could stall us past budget.  This is inherent to the PAM contract
 *  rather than a defect here: every module that speaks to the user has the
 *  same exposure, and an application that hosts us in-process can already do
 *  strictly worse than stalling.  Dropping the notice would buy nothing
 *  against that threat while making a silent multi-second pause at the sudo
 *  prompt look like a hang.
 *
 *
 *  MEMORY AND PARSING SAFETY
 *  -------------------------
 *  No heap allocation at all: every buffer is a bounded automatic array, so
 *  there is no allocation failure path and nothing to leak or double-free.
 *  No strcpy/strcat/sprintf/gets anywhere; only snprintf with checked return
 *  values, memcpy with pre-verified bounds, and explicit length tracking.
 *  The JSON reply is parsed by a hand-rolled scanner that never writes and
 *  never reads outside [buf, buf+len), understands string quoting so that a
 *  `"ok":true` appearing *inside* a string value cannot be mistaken for the
 *  real key, and refuses anything it does not fully understand.
 *
 *  The reply is never logged verbatim: only a length-capped, character-class
 *  sanitised copy of the `reason` token ever reaches syslog, so a malformed
 *  or hostile reply cannot inject newlines or control sequences into the
 *  system log.
 *
 *
 *  MODULE ARGUMENTS
 *  ----------------
 *      timeout=N   Hard wall-clock budget for the whole exchange, in seconds.
 *                  Accepts decimals.  Default 8, clamped to [1, 60].
 *      debug       Verbose pam_syslog(LOG_DEBUG) diagnostics.
 *      quiet       Never write anything to the user via the PAM conversation.
 *
 *  BUILD:   make            (see Makefile)
 *  INSTALL: /usr/lib/x86_64-linux-gnu/security/pam_iris.so   root:root 0644
 *
 *  Part of Iris.  Interface contract: SPEC.md v1.0.0.
 * ============================================================================
 */

#define _GNU_SOURCE /* SOCK_CLOEXEC, SOCK_NONBLOCK, MSG_NOSIGNAL */

#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <poll.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <sys/types.h>
#include <sys/un.h>
#include <syslog.h>
#include <time.h>
#include <unistd.h>

#include <security/pam_ext.h>
#include <security/pam_modules.h>

/* -------------------------------------------------------------------------
 * Tunables and hard limits.  All buffers are sized from these; none of them
 * is ever exceeded at runtime, and every one of them is enforced by a checked
 * bound rather than by convention.
 * ------------------------------------------------------------------------- */

/* Contract: SPEC.md "Paths" — root:root 0600 SOCK_STREAM. */
#define IRIS_SOCKET_PATH "/run/irisd/socket"

#define IRIS_DEFAULT_TIMEOUT_MS 8000L /* SPEC auth.timeout default of 8.0 s */
#define IRIS_MIN_TIMEOUT_MS 1000L
#define IRIS_MAX_TIMEOUT_MS 60000L

/*
 * The daemon is asked for slightly less time than we are prepared to wait, so
 * that a genuine "I looked for the whole window and saw nobody" answer still
 * arrives (and gets logged with its real reason) instead of being cut off by
 * our own deadline and reported as a transport timeout.
 */
#define IRIS_REPLY_MARGIN_MS 1000L

/* LOGIN_NAME_MAX is 256 on glibc; refuse anything longer outright. */
#define IRIS_MAX_USERNAME 256u
/* Worst case escape expansion is 6 bytes per byte (\u00XX), plus NUL. */
#define IRIS_ESCAPED_USER_MAX (IRIS_MAX_USERNAME * 6u + 1u)
#define IRIS_REQUEST_MAX 2048u

/*
 * SPEC.md caps a framed message at 64 KiB, but an `auth` reply is under 100
 * bytes.  8 KiB is generous headroom while keeping the whole thing on the
 * stack; a reply that does not fit is refused rather than truncated, because
 * a truncated JSON object could otherwise be misparsed.
 */
#define IRIS_REPLY_MAX 8192u
#define IRIS_MAX_REPLY_LINES 8 /* progress lines are not expected for `auth` */
#define IRIS_REASON_MAX 32u    /* longest reason word is "spoof_suspected" */

/* Internal status for the transport helpers, mapped to PAM codes at the end. */
typedef enum {
	IR_OK = 0,      /* the exchange completed; see the parsed reply */
	IR_UNAVAIL = 1, /* could not ask the question -> PAM_AUTHINFO_UNAVAIL */
	IR_FAIL = 2     /* asked and it went wrong    -> PAM_AUTH_ERR */
} ir_status;

struct iris_opts {
	long timeout_ms; /* hard wall-clock budget for the whole exchange */
	bool debug;
	bool quiet; /* suppress all conversation output */
};

/* =========================================================================
 * Small utilities
 * ========================================================================= */

/*
 * Monotonic milliseconds.  CLOCK_MONOTONIC is immune to settimeofday(2) and
 * NTP steps, which CLOCK_REALTIME is not: a clock jump must never be able to
 * extend (or silently expire) an authentication window.
 */
static ir_status now_ms(long *out)
{
	struct timespec ts;

	if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0)
		return IR_FAIL; /* cannot bound ourselves -> refuse to proceed */

	*out = (long)ts.tv_sec * 1000L + (long)(ts.tv_nsec / 1000000L);
	return IR_OK;
}

/* Milliseconds left before *deadline*; never negative. */
static long ms_remaining(long deadline_ms)
{
	long now = 0;

	if (now_ms(&now) != IR_OK)
		return 0; /* clock broken: treat as expired, i.e. fail closed */
	if (now >= deadline_ms)
		return 0;
	return deadline_ms - now;
}

static bool json_is_ws(char c)
{
	return c == ' ' || c == '\t' || c == '\r' || c == '\n';
}

/*
 * Reduce an untrusted string to a syslog-safe token in place.  Anything
 * outside [A-Za-z0-9_.-] becomes '.', so a hostile or corrupted reply cannot
 * inject newlines, escape sequences or fake log fields.  Applied to the
 * `reason` value before it is logged; the raw reply is never logged.
 */
static void sanitise_token(char *s)
{
	for (; *s != '\0'; s++) {
		unsigned char c = (unsigned char)*s;
		bool ok = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
			  (c >= '0' && c <= '9') || c == '_' || c == '-' || c == '.';
		if (!ok)
			*s = '.';
	}
}

/* =========================================================================
 * Minimal JSON reader
 *
 * Deliberately not a general parser.  It answers exactly two questions about
 * one flat object — "what is the value of this top-level key?" — and refuses
 * anything ambiguous.  It tracks nesting depth and string quoting so that a
 * key-looking substring inside a *value* (e.g. {"reason":"\"ok\":true"}) can
 * never be mistaken for the real member.  All access is bounds-checked
 * against len; the buffer is not required to be NUL-terminated.
 * ========================================================================= */

/*
 * Index just past the closing quote of the JSON string starting at *i* (which
 * must be its opening quote), or -1 if unterminated within len.
 */
static ptrdiff_t json_string_end(const char *b, size_t len, size_t i)
{
	if (i >= len || b[i] != '"')
		return -1;

	for (size_t j = i + 1; j < len; j++) {
		if (b[j] == '\\') {
			j++; /* skip the escaped character, whatever it is */
			continue;
		}
		if (b[j] == '"')
			return (ptrdiff_t)(j + 1);
	}
	return -1;
}

/*
 * Structural validation of one framed line: is it exactly ONE well-formed
 * JSON object, and nothing else?
 *
 * WHY THIS EXISTS
 * ---------------
 * json_find_member() below is a *scanner*, not a parser: it walks forward
 * looking for a key at depth 1 and returns the moment it finds one.  That
 * makes it fast and bounded, but on its own it will happily pull "ok":true out
 * of a line that is not a valid JSON object at all — a truncated object with
 * no closing brace, two concatenated objects, an object followed by trailing
 * garbage, or a line with junk before the opening brace.  Every one of those
 * was verified to be accepted before this function was added.
 *
 * None of them is reachable from today's daemon (the socket is root-only and
 * irisd frames its replies with json.dumps, which cannot emit any of these).
 * But "the only writer is currently well-behaved" is not a security property,
 * and a partial write, a future streaming change, or a daemon crash mid-frame
 * would turn a structurally broken frame into a *successful authentication*.
 * A gate that only holds while everything upstream is correct is not a gate.
 *
 * So: the whole line must validate as one balanced object before its "ok" is
 * allowed to mean anything.  This restores the property the file header
 * claims — "refuses anything it does not fully understand" — rather than
 * merely describing it.
 *
 * Bounded and non-recursive: one forward pass, an integer depth counter with
 * an explicit ceiling, and no writes.  Returns true only for a single object.
 */
#define IRIS_MAX_JSON_DEPTH 16

static bool json_line_is_object(const char *b, size_t len)
{
	/*
	 * A fixed-size stack of the open bracket *types*, not just a counter.
	 * A plain depth counter treats `]` as closing a `{`, which accepts
	 * `{"ok":true]` — verified, and exactly the kind of near-miss frame a
	 * truncated or buggy writer produces.  Bounded by the depth ceiling,
	 * so it is an automatic array with no allocation.
	 */
	char open[IRIS_MAX_JSON_DEPTH];
	size_t i = 0;
	int depth = 0;
	bool closed = false; /* the outermost object has been closed */

	while (i < len && json_is_ws(b[i]))
		i++;
	if (i >= len || b[i] != '{')
		return false; /* must begin with an object */

	for (; i < len; i++) {
		char c = b[i];

		if (c == '"') {
			ptrdiff_t end = json_string_end(b, len, i);

			if (end < 0)
				return false; /* unterminated string */
			i = (size_t)end - 1; /* loop's i++ lands after it */
			continue;
		}
		if (c == '{' || c == '[') {
			if (closed)
				return false; /* a second value after the object */
			if (depth >= IRIS_MAX_JSON_DEPTH)
				return false; /* absurdly nested: refuse */
			open[depth++] = c;
			continue;
		}
		if (c == '}' || c == ']') {
			char want;

			if (--depth < 0)
				return false; /* unbalanced close */
			want = (c == '}') ? '{' : '[';
			if (open[depth] != want)
				return false; /* `{...]` or `[...}` */
			if (depth == 0)
				closed = true;
			continue;
		}
		/*
		 * Outside a string, once the outermost object has closed the
		 * only thing allowed is trailing whitespace.  This is what
		 * rejects `{"ok":true} garbage` and `{"a":1},{"ok":true}`.
		 */
		if (closed && !json_is_ws(c))
			return false;
	}

	return closed && depth == 0;
}

/*
 * Offset of the first byte of the value of top-level member *key*, or -1 if
 * absent or the object is malformed.  Depth 1 == members of the outermost
 * object, so keys of nested objects are correctly ignored.
 *
 * Callers that act on the result MUST first satisfy themselves that the line
 * is a well-formed object (json_line_is_object); this function alone does not
 * establish that.
 */
static ptrdiff_t json_find_member(const char *b, size_t len, const char *key)
{
	size_t keylen = strlen(key);
	int depth = 0;

	for (size_t i = 0; i < len; i++) {
		char c = b[i];

		if (c == '{' || c == '[') {
			depth++;
			continue;
		}
		if (c == '}' || c == ']') {
			depth--;
			continue;
		}
		if (c != '"')
			continue;

		ptrdiff_t end = json_string_end(b, len, i);
		if (end < 0)
			return -1; /* unterminated string: refuse the object */

		size_t after = (size_t)end;
		while (after < len && json_is_ws(b[after]))
			after++;

		/* A string followed by ':' is a key; anything else is a value. */
		if (after < len && b[after] == ':' && depth == 1) {
			size_t content = (size_t)end - i - 2; /* minus both quotes */
			if (content == keylen &&
			    memcmp(b + i + 1, key, keylen) == 0) {
				size_t v = after + 1;
				while (v < len && json_is_ws(b[v]))
					v++;
				return (v < len) ? (ptrdiff_t)v : -1;
			}
		}

		i = (size_t)end - 1; /* resume after the string (loop adds 1) */
	}
	return -1;
}

/* True if the byte at *i* legitimately terminates a bare JSON literal. */
static bool json_token_ends(const char *b, size_t len, size_t i)
{
	if (i >= len)
		return true; /* end of the framed line */
	return b[i] == ',' || b[i] == '}' || b[i] == ']' || json_is_ws(b[i]);
}

/*
 * Read a boolean literal at *v*.  Returns 0 on success.  Anything that is not
 * exactly `true` or `false` (a number, a string "true", a truncated token) is
 * rejected — the success decision is never inferred from a fuzzy match.
 */
static int json_read_bool(const char *b, size_t len, size_t v, bool *out)
{
	if (v + 4 <= len && memcmp(b + v, "true", 4) == 0 &&
	    json_token_ends(b, len, v + 4)) {
		*out = true;
		return 0;
	}
	if (v + 5 <= len && memcmp(b + v, "false", 5) == 0 &&
	    json_token_ends(b, len, v + 5)) {
		*out = false;
		return 0;
	}
	return -1;
}

/*
 * Copy the JSON string value at *v* into *out* (NUL-terminated, at most
 * cap-1 bytes).  Returns 0 on success, -1 if the value is not a string, is
 * unterminated, contains an invalid escape, or is longer than we accept.
 *
 * This is used only for the diagnostic `reason`, so a failure here degrades
 * the log message; it can never turn a failure into a success.
 */
static int json_read_string(const char *b, size_t len, size_t v, char *out, size_t cap)
{
	size_t o = 0;

	if (cap == 0)
		return -1;
	out[0] = '\0';
	if (v >= len || b[v] != '"')
		return -1;

	for (size_t i = v + 1; i < len; i++) {
		char c = b[i];

		if (c == '"') {
			out[o] = '\0';
			return 0;
		}
		if (c == '\\') {
			if (++i >= len)
				return -1;
			switch (b[i]) {
			case '"':  c = '"';  break;
			case '\\': c = '\\'; break;
			case '/':  c = '/';  break;
			case 'b':  c = '\b'; break;
			case 'f':  c = '\f'; break;
			case 'n':  c = '\n'; break;
			case 'r':  c = '\r'; break;
			case 't':  c = '\t'; break;
			case 'u':
				/*
				 * The reason vocabulary (SPEC.md) is plain
				 * lowercase ASCII, so a \uXXXX escape is never
				 * legitimate here.  Consume it and substitute a
				 * placeholder that cannot be confused with a
				 * real reason token rather than implementing
				 * UTF-16 surrogate decoding in a PAM module.
				 */
				if (i + 4 >= len)
					return -1;
				i += 4;
				c = '?';
				break;
			default:
				return -1; /* invalid escape: refuse */
			}
		}
		if (o + 1 >= cap)
			return -1; /* longer than we are willing to hold */
		out[o++] = c;
	}
	return -1; /* unterminated */
}

/*
 * Escape *in* into *out* as the body of a JSON string.  Returns 0 on success,
 * -1 if it would not fit.  Bytes >= 0x80 are passed through unchanged: a UTF-8
 * username stays valid UTF-8, and an invalid one is rejected by the daemon's
 * decoder (which fails closed) rather than being silently mangled here.
 */
static int json_escape(const char *in, char *out, size_t cap)
{
	size_t o = 0;

	if (cap == 0)
		return -1;

	for (const unsigned char *p = (const unsigned char *)in; *p != '\0'; p++) {
		char scratch[7];
		const char *rep = NULL;

		switch (*p) {
		case '"':  rep = "\\\""; break;
		case '\\': rep = "\\\\"; break;
		case '\n': rep = "\\n";  break;
		case '\r': rep = "\\r";  break;
		case '\t': rep = "\\t";  break;
		default:
			if (*p < 0x20) {
				int n = snprintf(scratch, sizeof(scratch),
						 "\\u%04x", (unsigned)*p);
				if (n < 0 || (size_t)n >= sizeof(scratch))
					return -1;
				rep = scratch;
			}
			break;
		}

		if (rep != NULL) {
			size_t l = strlen(rep);
			if (o + l + 1 > cap)
				return -1;
			memcpy(out + o, rep, l);
			o += l;
		} else {
			if (o + 2 > cap)
				return -1;
			out[o++] = (char)*p;
		}
	}
	out[o] = '\0';
	return 0;
}

/* =========================================================================
 * Module arguments
 * ========================================================================= */

static void parse_args(pam_handle_t *pamh, int argc, const char **argv,
		       struct iris_opts *o)
{
	o->timeout_ms = IRIS_DEFAULT_TIMEOUT_MS;
	o->debug = false;
	o->quiet = false;

	for (int i = 0; i < argc; i++) {
		const char *a = argv[i];

		if (a == NULL)
			continue;

		if (strncmp(a, "timeout=", 8) == 0) {
			const char *val = a + 8;
			char *end = NULL;
			double secs;

			errno = 0;
			secs = strtod(val, &end);
			if (end == val || end == NULL || *end != '\0' ||
			    errno == ERANGE || !(secs == secs) /* NaN */) {
				pam_syslog(pamh, LOG_WARNING,
					   "ignoring malformed option '%s'; using %ld ms",
					   a, o->timeout_ms);
				continue;
			}

			long ms = (long)(secs * 1000.0);
			if (ms < IRIS_MIN_TIMEOUT_MS)
				ms = IRIS_MIN_TIMEOUT_MS;
			if (ms > IRIS_MAX_TIMEOUT_MS)
				ms = IRIS_MAX_TIMEOUT_MS;
			/*
			 * Clamping rather than rejecting: an admin typo must
			 * not be able to configure an unbounded wait, and must
			 * not disable face auth outright either.
			 */
			o->timeout_ms = ms;
		} else if (strcmp(a, "debug") == 0) {
			o->debug = true;
		} else if (strcmp(a, "quiet") == 0) {
			o->quiet = true;
		} else {
			/*
			 * Never fail on an unknown option: a typo in
			 * /etc/pam.d must degrade to "face auth behaves
			 * normally", not to a broken auth stack.
			 */
			pam_syslog(pamh, LOG_WARNING, "ignoring unknown option '%s'", a);
		}
	}
}

/* =========================================================================
 * Transport
 * ========================================================================= */

/*
 * Re-arm both socket timeouts from the time left on the wall-clock deadline.
 * Called before every send/recv so that a peer cannot extend the exchange by
 * stalling between syscalls.  A zero timeval means "block forever" to the
 * kernel, so an expired budget is reported as an error instead of being set.
 */
static ir_status arm_timeouts(int fd, long deadline_ms)
{
	long left = ms_remaining(deadline_ms);
	struct timeval tv;

	if (left <= 0)
		return IR_FAIL;

	tv.tv_sec = left / 1000L;
	tv.tv_usec = (left % 1000L) * 1000L;

	if (setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv)) != 0)
		return IR_FAIL;
	if (setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv)) != 0)
		return IR_FAIL;
	return IR_OK;
}

/*
 * Connect to the daemon socket, bounded by *deadline_ms*.
 *
 * The connect is done non-blocking + poll() because connect(2) on a UNIX
 * socket whose accept backlog is full *blocks*, and SO_SNDTIMEO does not
 * apply to it.  Without this, a wedged daemon holding a full backlog would
 * hang every sudo on the machine.  The descriptor is switched back to
 * blocking afterwards so the SO_*TIMEO-bounded I/O helpers can be used.
 */
static ir_status iris_connect(pam_handle_t *pamh, const struct iris_opts *o,
			      long deadline_ms, int *out_fd)
{
	struct sockaddr_un addr;
	size_t path_len = strlen(IRIS_SOCKET_PATH);
	int fd;
	int flags;

	*out_fd = -1;

	/* Compile-time truth, checked anyway: sun_path is not NUL-safe if full. */
	if (path_len >= sizeof(addr.sun_path)) {
		pam_syslog(pamh, LOG_ERR, "socket path too long: " IRIS_SOCKET_PATH);
		return IR_UNAVAIL;
	}

	/* SOCK_CLOEXEC: never leak this descriptor into a shell we authenticate. */
	fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC | SOCK_NONBLOCK, 0);
	if (fd < 0) {
		pam_syslog(pamh, LOG_ERR, "socket() failed: %m");
		return IR_UNAVAIL;
	}

	memset(&addr, 0, sizeof(addr));
	addr.sun_family = AF_UNIX;
	memcpy(addr.sun_path, IRIS_SOCKET_PATH, path_len + 1);

	if (connect(fd, (const struct sockaddr *)&addr, sizeof(addr)) != 0) {
		if (errno != EINPROGRESS && errno != EAGAIN &&
		    errno != EWOULDBLOCK && errno != EINTR) {
			/*
			 * ENOENT/ECONNREFUSED: irisd is not running.
			 * EACCES: the socket is root-only (SPEC: 0600 root:root)
			 * and this PAM stack is not running with privilege.
			 * All of these mean "cannot ask", not "answer is no".
			 */
			if (o->debug || (errno != ENOENT && errno != ECONNREFUSED))
				pam_syslog(pamh, LOG_NOTICE,
					   "cannot reach irisd at " IRIS_SOCKET_PATH ": %m");
			close(fd);
			return IR_UNAVAIL;
		}

		for (;;) {
			long left = ms_remaining(deadline_ms);
			struct pollfd pfd;
			int rc;
			int soerr = 0;
			socklen_t soerr_len = sizeof(soerr);

			if (left <= 0) {
				pam_syslog(pamh, LOG_NOTICE,
					   "timed out connecting to irisd");
				close(fd);
				return IR_UNAVAIL;
			}

			pfd.fd = fd;
			pfd.events = POLLOUT;
			pfd.revents = 0;

			rc = poll(&pfd, 1, (int)(left > (long)INT_MAX ? INT_MAX : left));
			if (rc < 0) {
				if (errno == EINTR)
					continue; /* re-derive the budget and retry */
				pam_syslog(pamh, LOG_ERR, "poll() failed: %m");
				close(fd);
				return IR_UNAVAIL;
			}
			if (rc == 0)
				continue; /* loop re-checks the deadline above */

			if (getsockopt(fd, SOL_SOCKET, SO_ERROR, &soerr, &soerr_len) != 0) {
				pam_syslog(pamh, LOG_ERR, "getsockopt(SO_ERROR) failed: %m");
				close(fd);
				return IR_UNAVAIL;
			}
			if (soerr != 0) {
				if (o->debug)
					pam_syslog(pamh, LOG_DEBUG,
						   "connect to irisd failed: %s",
						   strerror(soerr));
				close(fd);
				return IR_UNAVAIL;
			}
			break; /* connected */
		}
	}

	/* Back to blocking; SO_RCVTIMEO/SO_SNDTIMEO bound every operation. */
	flags = fcntl(fd, F_GETFL, 0);
	if (flags < 0 || fcntl(fd, F_SETFL, flags & ~O_NONBLOCK) != 0) {
		pam_syslog(pamh, LOG_ERR, "fcntl() failed on irisd socket: %m");
		close(fd);
		return IR_UNAVAIL;
	}

	*out_fd = fd;
	return IR_OK;
}

/*
 * Write the whole request.  MSG_NOSIGNAL is essential: without it a daemon
 * that closes the connection early would deliver SIGPIPE to the *application*
 * (sudo, gdm, sshd), whose default disposition is to terminate it.  A PAM
 * module must never be able to kill its host.
 */
static ir_status send_all(pam_handle_t *pamh, int fd, const char *buf, size_t len,
			  long deadline_ms)
{
	size_t off = 0;

	while (off < len) {
		ssize_t n;

		if (arm_timeouts(fd, deadline_ms) != IR_OK) {
			pam_syslog(pamh, LOG_NOTICE, "deadline expired before request was sent");
			return IR_FAIL;
		}

		n = send(fd, buf + off, len - off, MSG_NOSIGNAL);
		if (n < 0) {
			if (errno == EINTR)
				continue;
			if (errno == EAGAIN || errno == EWOULDBLOCK) {
				pam_syslog(pamh, LOG_NOTICE, "timed out sending request to irisd");
				return IR_FAIL;
			}
			pam_syslog(pamh, LOG_ERR, "send() to irisd failed: %m");
			return IR_FAIL;
		}
		if (n == 0) {
			pam_syslog(pamh, LOG_ERR, "irisd accepted no data");
			return IR_FAIL;
		}
		off += (size_t)n;
	}
	return IR_OK;
}

/*
 * Read framed lines until one carries a top-level "ok", and report its value.
 *
 * Bounded four ways: the fixed buffer, the line count, the wall-clock
 * deadline, and the per-syscall socket timeout.  A line that fills the buffer
 * without a newline, a peer that closes mid-message, or a message without a
 * usable "ok" are all failures — never a silent success.
 *
 * `reason_out` is best-effort diagnostic only and is empty when absent.
 */
static ir_status read_reply(pam_handle_t *pamh, const struct iris_opts *o, int fd,
			    long deadline_ms, bool *ok_out, char *reason_out,
			    size_t reason_cap)
{
	char buf[IRIS_REPLY_MAX];
	size_t used = 0;
	int lines = 0;

	*ok_out = false;
	if (reason_cap > 0)
		reason_out[0] = '\0';

	for (;;) {
		ssize_t n;

		/* Drain every complete line already in the buffer. */
		while (used > 0) {
			const char *nl = memchr(buf, '\n', used);
			size_t line_len;
			ptrdiff_t v;

			if (nl == NULL)
				break;

			line_len = (size_t)(nl - buf);

			if (++lines > IRIS_MAX_REPLY_LINES) {
				pam_syslog(pamh, LOG_ERR,
					   "irisd sent more than %d messages; refusing",
					   IRIS_MAX_REPLY_LINES);
				return IR_FAIL;
			}

			/*
			 * Structure before content.  A line that is not one
			 * well-formed JSON object is refused outright: we will
			 * not read an authentication verdict out of a frame we
			 * cannot fully account for.  Refusing (rather than
			 * skipping) is deliberate — irisd is root-owned and
			 * should never emit one, so a malformed frame means
			 * something is wrong enough to fall back to a password.
			 */
			if (!json_line_is_object(buf, line_len)) {
				pam_syslog(pamh, LOG_ERR,
					   "irisd sent a %zu-byte frame that is not a "
					   "well-formed JSON object; refusing",
					   line_len);
				return IR_FAIL;
			}

			v = json_find_member(buf, line_len, "ok");
			if (v >= 0) {
				bool ok = false;

				if (json_read_bool(buf, line_len, (size_t)v, &ok) != 0) {
					pam_syslog(pamh, LOG_ERR,
						   "irisd reply has a non-boolean \"ok\"; refusing");
					return IR_FAIL;
				}

				ptrdiff_t r = json_find_member(buf, line_len, "reason");
				if (r >= 0 &&
				    json_read_string(buf, line_len, (size_t)r,
						     reason_out, reason_cap) != 0) {
					/*
					 * A reason we cannot represent is a
					 * logging problem, not an authentication
					 * decision: `ok` alone decides.
					 */
					if (reason_cap > 0)
						reason_out[0] = '\0';
				}
				/*
				 * Guarded: with reason_cap == 0 nothing above
				 * ever wrote a NUL, so sanitise_token() would
				 * walk uninitialised stack looking for one.
				 * Today every caller passes 32, but this
				 * function's contract allows 0.
				 */
				if (reason_cap > 0)
					sanitise_token(reason_out);

				*ok_out = ok;
				return IR_OK;
			}

			/*
			 * No "ok": per SPEC.md this is an enrolment progress
			 * line, which `auth` never emits.  Discard it, count it
			 * against the line budget, and keep looking.
			 */
			if (o->debug)
				pam_syslog(pamh, LOG_DEBUG,
					   "discarding a %zu-byte irisd message with no \"ok\"",
					   line_len);

			memmove(buf, buf + line_len + 1, used - line_len - 1);
			used -= line_len + 1;
		}

		if (used >= sizeof(buf)) {
			pam_syslog(pamh, LOG_ERR,
				   "irisd reply exceeds %zu bytes with no newline; refusing",
				   sizeof(buf));
			return IR_FAIL;
		}

		if (arm_timeouts(fd, deadline_ms) != IR_OK) {
			pam_syslog(pamh, LOG_NOTICE, "timed out waiting for irisd");
			return IR_FAIL;
		}

		n = recv(fd, buf + used, sizeof(buf) - used, 0);
		if (n < 0) {
			if (errno == EINTR)
				continue;
			if (errno == EAGAIN || errno == EWOULDBLOCK) {
				pam_syslog(pamh, LOG_NOTICE, "timed out waiting for irisd");
				return IR_FAIL;
			}
			pam_syslog(pamh, LOG_ERR, "recv() from irisd failed: %m");
			return IR_FAIL;
		}
		if (n == 0) {
			pam_syslog(pamh, LOG_ERR,
				   "irisd closed the connection without a complete reply");
			return IR_FAIL;
		}
		used += (size_t)n;
	}
}

/* =========================================================================
 * Reply interpretation
 * ========================================================================= */

/*
 * Map a failure reason to a PAM code.  "Could not ask" reasons become
 * PAM_AUTHINFO_UNAVAIL so that they are visibly distinct in the logs from a
 * user who was looked at and rejected; everything else is PAM_AUTH_ERR.
 * Neither is PAM_SUCCESS, so an unknown reason is safe by construction.
 */
static int pam_code_for_reason(const char *reason)
{
	if (strcmp(reason, "disabled") == 0 || strcmp(reason, "not_enrolled") == 0)
		return PAM_AUTHINFO_UNAVAIL;
	return PAM_AUTH_ERR;
}

/*
 * A short, fixed message for the user, chosen from *our* table by reason code.
 * The daemon's own string is never echoed to the terminal, so a malformed
 * reply cannot print arbitrary text into a login prompt.  Returns NULL when
 * nothing should be said.
 */
static const char *user_message_for_reason(const char *reason)
{
	if (strcmp(reason, "no_match") == 0)
		return "Iris: face not recognised.";
	if (strcmp(reason, "no_face") == 0)
		return "Iris: no face detected.";
	if (strcmp(reason, "timeout") == 0)
		return "Iris: timed out.";
	if (strcmp(reason, "camera_error") == 0)
		return "Iris: infrared camera unavailable.";
	if (strcmp(reason, "spoof_suspected") == 0)
		return "Iris: image did not look like a live face.";
	if (strcmp(reason, "lockout") == 0)
		return "Iris: too many attempts; face unlock temporarily locked.";
	/*
	 * "disabled" and "not_enrolled" are normal states, not errors — saying
	 * anything would just be noise on every single sudo.
	 */
	return NULL;
}

/* =========================================================================
 * PAM entry points
 * ========================================================================= */

int pam_sm_authenticate(pam_handle_t *pamh, int flags, int argc, const char **argv)
{
	struct iris_opts o;
	const char *user = NULL;
	char escaped[IRIS_ESCAPED_USER_MAX];
	char request[IRIS_REQUEST_MAX];
	char reason[IRIS_REASON_MAX];
	long deadline_ms = 0;
	long start_ms = 0;
	long daemon_ms;
	int req_len;
	int fd = -1;
	int rc;
	bool authenticated = false;
	bool speak;

	/*
	 * FAIL CLOSED: the return value starts as a denial and is only ever
	 * changed by the single branch that observed an explicit "ok":true.
	 */
	int rv = PAM_AUTH_ERR;

	parse_args(pamh, argc, argv, &o);

	/*
	 * PAM_SILENT is the application saying "do not talk to the user".
	 * PAM's flags are signed ints but the bit constants are unsigned, so
	 * the mask is done in unsigned arithmetic to keep -Wsign-conversion
	 * quiet without changing the meaning of the test.
	 */
	speak = !o.quiet && ((unsigned int)flags & (unsigned int)PAM_SILENT) == 0u;

	/*
	 * pam_get_user() may call the application's conversation function to
	 * prompt for a name.  That is the application's I/O and is not bounded
	 * by us, so the deadline starts *after* it returns: a user typing
	 * slowly must not eat the camera's time budget.
	 */
	rc = pam_get_user(pamh, &user, NULL);
	if (rc != PAM_SUCCESS || user == NULL || user[0] == '\0') {
		pam_syslog(pamh, LOG_NOTICE, "cannot determine the user: %s",
			   pam_strerror(pamh, rc));
		return PAM_AUTH_ERR;
	}

	/*
	 * Validate before escaping.  A username containing control characters
	 * cannot be a real account on this system; refusing outright is
	 * cheaper and safer than reasoning about how it round-trips through
	 * JSON, the daemon and the template store.
	 */
	{
		size_t ulen = strnlen(user, IRIS_MAX_USERNAME + 1);

		if (ulen > IRIS_MAX_USERNAME) {
			pam_syslog(pamh, LOG_WARNING,
				   "refusing a username longer than %u bytes",
				   IRIS_MAX_USERNAME);
			return PAM_AUTH_ERR;
		}
		for (size_t i = 0; i < ulen; i++) {
			unsigned char c = (unsigned char)user[i];

			if (c < 0x20 || c == 0x7f) {
				pam_syslog(pamh, LOG_WARNING,
					   "refusing a username containing control characters");
				return PAM_AUTH_ERR;
			}
		}
	}

	if (json_escape(user, escaped, sizeof(escaped)) != 0) {
		pam_syslog(pamh, LOG_WARNING, "username does not fit the request buffer");
		return PAM_AUTH_ERR;
	}

	if (now_ms(&start_ms) != IR_OK) {
		pam_syslog(pamh, LOG_ERR, "CLOCK_MONOTONIC unavailable: %m");
		return PAM_AUTH_ERR; /* cannot bound ourselves -> refuse */
	}
	deadline_ms = start_ms + o.timeout_ms;

	/* Leave ourselves a margin to receive the daemon's own timeout verdict. */
	daemon_ms = o.timeout_ms - IRIS_REPLY_MARGIN_MS;
	if (daemon_ms < 500L)
		daemon_ms = o.timeout_ms / 2L;

	/*
	 * Built with snprintf and an integral seconds.milliseconds rendering.
	 * "%f" is NOT used on purpose: it honours LC_NUMERIC, and under a
	 * locale such as de_DE it would emit "7,000" — syntactically invalid
	 * JSON that the daemon would reject on every login.
	 */
	req_len = snprintf(request, sizeof(request),
			   "{\"op\":\"auth\",\"user\":\"%s\",\"timeout\":%ld.%03ld}\n",
			   escaped, daemon_ms / 1000L, daemon_ms % 1000L);
	if (req_len < 0 || (size_t)req_len >= sizeof(request)) {
		pam_syslog(pamh, LOG_WARNING, "could not build the auth request");
		return PAM_AUTH_ERR;
	}

	if (o.debug)
		pam_syslog(pamh, LOG_DEBUG,
			   "authenticating user '%s' with a %ld ms budget (%ld ms for irisd)",
			   user, o.timeout_ms, daemon_ms);

	/*
	 * Connect first, announce second.  If irisd is not running we fall
	 * through to the password prompt without ever having told the user to
	 * look at a camera that was never going to be used.
	 */
	switch (iris_connect(pamh, &o, deadline_ms, &fd)) {
	case IR_OK:
		break;
	case IR_UNAVAIL:
		return PAM_AUTHINFO_UNAVAIL;
	default:
		return PAM_AUTH_ERR;
	}

	/*
	 * Tell the user something is happening.  Without this a `sudo` that
	 * spends several seconds looking at the camera before printing its
	 * password prompt looks like a hung terminal.  pam_prompt with
	 * PAM_TEXT_INFO does not wait for input.
	 */
	if (speak)
		(void)pam_prompt(pamh, PAM_TEXT_INFO, NULL, "%s",
				 "Iris: look at the infrared camera...");

	if (send_all(pamh, fd, request, (size_t)req_len, deadline_ms) != IR_OK) {
		close(fd);
		return PAM_AUTH_ERR;
	}

	reason[0] = '\0';
	if (read_reply(pamh, &o, fd, deadline_ms, &authenticated, reason,
		       sizeof(reason)) != IR_OK) {
		close(fd);
		return PAM_AUTH_ERR;
	}
	close(fd);
	fd = -1;

	if (authenticated) {
		/*
		 * THE ONLY SUCCESS PATH IN THIS FILE.  Reached solely from a
		 * well-formed reply carrying a top-level "ok":true.
		 */
		rv = PAM_SUCCESS;
		if (o.debug)
			pam_syslog(pamh, LOG_DEBUG, "face authentication succeeded for '%s'",
				   user);
		else
			pam_syslog(pamh, LOG_INFO, "face authentication succeeded for '%s'",
				   user);
	} else {
		const char *msg;

		rv = pam_code_for_reason(reason);

		/* reason[] is sanitised; the raw reply is never logged. */
		pam_syslog(pamh, LOG_NOTICE, "face authentication failed for '%s' (reason=%s)",
			   user, reason[0] != '\0' ? reason : "unspecified");

		msg = user_message_for_reason(reason);
		if (speak && msg != NULL)
			(void)pam_prompt(pamh, PAM_TEXT_INFO, NULL, "%s", msg);
	}

	if (o.debug) {
		long left = ms_remaining(deadline_ms);

		pam_syslog(pamh, LOG_DEBUG, "returning %s after %ld ms",
			   pam_strerror(pamh, rv), o.timeout_ms - left);
	}

	/*
	 * Belt and braces.  If any future edit ever lets rv reach PAM_SUCCESS
	 * without an affirmative daemon verdict, this converts it back into a
	 * denial rather than into a security incident.
	 */
	if (rv == PAM_SUCCESS && !authenticated)
		rv = PAM_AUTH_ERR;

	return rv;
}

/*
 * Face authentication establishes no credentials of its own — no tickets, no
 * keyring, no group memberships — so there is nothing to set, delete or
 * refresh.
 *
 * WHY PAM_IGNORE AND NOT PAM_SUCCESS
 * ----------------------------------
 * This is not a cosmetic choice, and PAM_SUCCESS here is an active bug.
 *
 * libpam applies the SAME control value to the setcred phase as to the auth
 * phase.  Our required stacking is
 *
 *     auth  [success=done default=ignore]  pam_iris.so
 *     auth  ...                            pam_unix.so, pam_gnome_keyring.so, …
 *
 * so if this function returns PAM_SUCCESS, `success=done` fires during
 * pam_setcred() and the credential phase of every module *below* us is
 * skipped.  That happens on EVERY login, including plain password logins that
 * never touched the camera, because setcred re-runs the whole stack
 * independently of what authenticate did.  The visible damage on this target
 * (GNOME/gdm) is pam_gnome_keyring never getting its setcred call, so the
 * login keyring stays locked and the user is re-prompted for it forever;
 * pam_krb5/pam_sss stacks would silently lose their tickets the same way.
 *
 * PAM_IGNORE is handled specially by libpam's dispatcher: the module is
 * dropped from the stack's result entirely and evaluation continues to the
 * next module.  It does not consult the `success=` jump, so the rest of the
 * credential phase runs exactly as it did before Iris was installed — which
 * is the whole point of SAFETY rule 5.
 *
 * This is safe because SPEC.md and the installer both forbid pam_iris.so from
 * being the only module in a stack; PAM_IGNORE from a lone module would leave
 * the stack with no result, but that configuration is already prohibited for
 * far more serious reasons (it would remove the password fallback).
 */
int pam_sm_setcred(pam_handle_t *pamh, int flags, int argc, const char **argv)
{
	(void)pamh;
	(void)flags;
	(void)argc;
	(void)argv;
	return PAM_IGNORE;
}
