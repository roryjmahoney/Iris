/*
 * Runtime contract of pam_iris.c against a scripted fake irisd.
 *
 * The real module source is compiled in with IRIS_PAM_TEST_SOCKET pointing at
 * a temporary socket, and the handful of libpam calls it makes are stubbed so
 * no live PAM stack, daemon or camera is involved.  Each case forks a child
 * that plays the daemon (reply, silence, dribble, early close, ...) and checks
 * the PAM code, the elapsed time, what was sent and what reached the user and
 * the log.
 */
#define _GNU_SOURCE
#include <signal.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <time.h>
#include <security/pam_ext.h>
#include <security/pam_modules.h>

struct pam_handle {
	char *marker;
	char *capture;
};

static const char *current_user = "alice";
static int prompt_delay_ms; /* lets a test act between connect() and send() */
static char prompts[4096];
static char last_log[1024];

int pam_get_data(const pam_handle_t *p, const char *key, const void **v)
{
	*v = strstr(key, "face") ? p->marker : p->capture;
	return PAM_SUCCESS;
}

int pam_set_data(pam_handle_t *p, const char *key, void *v,
		 void (*cleanup)(pam_handle_t *, void *, int))
{
	(void)cleanup;
	char **slot = strstr(key, "face") ? &p->marker : &p->capture;
	free(*slot);
	*slot = v;
	return PAM_SUCCESS;
}

int pam_get_item(const pam_handle_t *p, int item, const void **v)
{
	(void)p;
	*v = item == PAM_SERVICE ? "sudo" : current_user;
	return PAM_SUCCESS;
}

int pam_get_user(pam_handle_t *p, const char **user, const char *prompt)
{
	(void)p;
	(void)prompt;
	*user = current_user;
	return PAM_SUCCESS;
}

void pam_syslog(const pam_handle_t *p, int priority, const char *fmt, ...)
{
	va_list ap;

	(void)p;
	(void)priority;
	va_start(ap, fmt);
	vsnprintf(last_log, sizeof(last_log), fmt, ap);
	va_end(ap);
}

int pam_prompt(pam_handle_t *p, int style, char **response, const char *fmt, ...)
{
	char line[512];
	va_list ap;

	(void)p;
	(void)style;
	if (response != NULL)
		*response = NULL;
	va_start(ap, fmt);
	vsnprintf(line, sizeof(line), fmt, ap);
	va_end(ap);
	strncat(prompts, line, sizeof(prompts) - strlen(prompts) - 2);
	strcat(prompts, "|");
	if (prompt_delay_ms > 0) {
		struct timespec ts = {0, (long)prompt_delay_ms * 1000000L};
		nanosleep(&ts, NULL);
	}
	return PAM_SUCCESS;
}

#include "../pam/pam_iris.c"

/* ------------------------------------------------------------------------ */

static int failures;

#define CHECK(cond, ...)                                                      \
	do {                                                                  \
		if (!(cond)) {                                                \
			failures++;                                           \
			fprintf(stderr, "FAIL %s:%d: ", __FILE__, __LINE__);  \
			fprintf(stderr, __VA_ARGS__);                         \
			fputc('\n', stderr);                                  \
		}                                                             \
	} while (0)

struct script {
	const char *reply;   /* bytes to send back (may lack a newline) */
	size_t reply_len;    /* 0: strlen(reply) */
	bool no_accept;      /* leave the connection in the backlog */
	bool no_read;        /* close without reading the request */
	int drip_ms;         /* send the reply one byte at a time */
	int hold_ms;         /* stay connected this long before closing */
	int backlog;         /* listen() backlog; 0 means 1 */
	int prefill;         /* extra connections queued before the module's */
};

struct result {
	int rc;
	long elapsed_ms;
	char request[2048];
};

static long monotonic_ms(void)
{
	struct timespec ts;

	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (long)ts.tv_sec * 1000L + ts.tv_nsec / 1000000L;
}

static void sleep_ms(int ms)
{
	struct timespec ts = {ms / 1000, (long)(ms % 1000) * 1000000L};

	while (nanosleep(&ts, &ts) != 0)
		;
}

static int bind_server(int backlog)
{
	struct sockaddr_un a = {.sun_family = AF_UNIX};
	int s = socket(AF_UNIX, SOCK_STREAM, 0);

	memcpy(a.sun_path, IRIS_SOCKET_PATH, strlen(IRIS_SOCKET_PATH) + 1);
	unlink(IRIS_SOCKET_PATH);
	if (s < 0 || bind(s, (struct sockaddr *)&a, sizeof(a)) != 0 || listen(s, backlog) != 0) {
		perror("fake irisd");
		exit(2);
	}
	return s;
}

static void child_daemon(int server, int report, const struct script *sc)
{
	char buf[2048];
	size_t used = 0;
	const char *reply = sc->reply ? sc->reply : "";
	size_t len = sc->reply_len ? sc->reply_len : strlen(reply);
	int c;

	if (sc->no_accept) {
		sleep_ms(sc->hold_ms);
		_exit(0);
	}
	c = accept(server, NULL, NULL);
	if (c < 0)
		_exit(1);
	if (!sc->no_read) {
		while (used < sizeof(buf) - 1) {
			ssize_t n = read(c, buf + used, sizeof(buf) - 1 - used);
			if (n <= 0)
				break;
			used += (size_t)n;
			if (memchr(buf, '\n', used))
				break;
		}
		if (write(report, buf, used) < 0)
			_exit(1);
	}
	close(report);
	if (sc->drip_ms > 0) {
		for (size_t i = 0; i < len; i++) {
			if (send(c, reply + i, 1, MSG_NOSIGNAL) != 1)
				break;
			sleep_ms(sc->drip_ms);
		}
	} else if (len > 0) {
		if (send(c, reply, len, MSG_NOSIGNAL) < 0)
			_exit(0);
	}
	sleep_ms(sc->hold_ms);
	close(c);
	_exit(0);
}

static struct result run(const struct script *sc, int argc, const char **argv, int flags)
{
	struct result r = {0};
	int report[2];
	int extra[8];
	pam_handle_t p = {0};
	int server;
	pid_t child;
	long start;

	if (pipe(report) != 0)
		exit(2);
	server = bind_server(sc->backlog > 0 ? sc->backlog : 1);
	for (int i = 0; i < sc->prefill && i < 8; i++) {
		struct sockaddr_un a = {.sun_family = AF_UNIX};
		extra[i] = socket(AF_UNIX, SOCK_STREAM | SOCK_NONBLOCK, 0);
		memcpy(a.sun_path, IRIS_SOCKET_PATH, strlen(IRIS_SOCKET_PATH) + 1);
		(void)connect(extra[i], (struct sockaddr *)&a, sizeof(a));
	}

	child = fork();
	if (child == 0) {
		close(report[0]);
		child_daemon(server, report[1], sc);
	}
	close(report[1]);

	prompts[0] = '\0';
	last_log[0] = '\0';
	start = monotonic_ms();
	r.rc = pam_sm_authenticate(&p, flags, argc, argv);
	r.elapsed_ms = monotonic_ms() - start;

	kill(child, SIGKILL);
	waitpid(child, NULL, 0);
	ssize_t n = read(report[0], r.request, sizeof(r.request) - 1);
	r.request[n > 0 ? n : 0] = '\0';
	close(report[0]);
	for (int i = 0; i < sc->prefill && i < 8; i++)
		close(extra[i]);
	close(server);
	unlink(IRIS_SOCKET_PATH);
	free(p.marker);
	free(p.capture);
	return r;
}

static struct result reply(const char *text)
{
	struct script sc = {.reply = text};
	const char *argv[] = {"timeout=2"};

	return run(&sc, 1, argv, PAM_SILENT);
}

/* ------------------------------------------------------------------------ */

static void test_verdicts(void)
{
	struct { const char *reply; int want; } cases[] = {
		{"{\"ok\":true,\"confidence\":0.9,\"reason\":\"match\",\"face\":\"default\"}\n", PAM_SUCCESS},
		{"{\"ok\":false,\"reason\":\"no_match\"}\n", PAM_AUTH_ERR},
		{"{\"ok\":false,\"reason\":\"spoof_suspected\"}\n", PAM_AUTH_ERR},
		{"{\"ok\":false,\"reason\":\"lockout\",\"retry_after\":42.0}\n", PAM_AUTH_ERR},
		{"{\"ok\":false,\"reason\":\"timeout\"}\n", PAM_AUTH_ERR},
		{"{\"ok\":false,\"reason\":\"camera_error\"}\n", PAM_AUTH_ERR},
		{"{\"ok\":false,\"reason\":\"never_heard_of_it\"}\n", PAM_AUTH_ERR},
		{"{\"ok\":false}\n", PAM_AUTH_ERR},
		/* "could not ask": must not count as a failed attempt */
		{"{\"ok\":false,\"reason\":\"disabled\"}\n", PAM_AUTHINFO_UNAVAIL},
		{"{\"ok\":false,\"reason\":\"not_enrolled\"}\n", PAM_AUTHINFO_UNAVAIL},
		/* whitespace and key order are irrelevant */
		{"  { \"reason\" : \"match\" , \"ok\" : true }  \r\n", PAM_SUCCESS},
		/* the full value grammar is understood, not just what irisd sends today */
		{"{\"ok\":true,\"n\":[0,-1,2.5,-0.25e+3,1E-2,true,false,null],\"o\":{},\"a\":[],"
		 "\"s\":\"q\\\"\\\\\\u00e9\",\"d\":{\"k\":[{\"z\":1}]}}\n", PAM_SUCCESS},
	};

	for (size_t i = 0; i < sizeof(cases) / sizeof(cases[0]); i++) {
		struct result r = reply(cases[i].reply);
		CHECK(r.rc == cases[i].want, "verdict %s -> %d, want %d", cases[i].reply, r.rc, cases[i].want);
	}
}

static void test_malformed_replies_never_authenticate(void)
{
	char big[IRIS_REPLY_MAX + 16];
	char deep[64];
	char lines[1024] = "";
	const char *cases[] = {
		"{\"ok\":true",                     /* truncated, then closed */
		"{\"ok\":true\n",                   /* unbalanced */
		"{\"ok\":true]\n",                  /* mismatched close */
		"{\"ok\":true}garbage\n",           /* trailing junk */
		"{\"ok\":true}{\"ok\":true}\n",     /* two objects */
		"junk{\"ok\":true}\n",              /* leading junk */
		"[{\"ok\":true}]\n",                /* not an object */
		"{\"ok\":\"true\"}\n",              /* string, not bool */
		"{\"ok\":1}\n",
		"{\"ok\":null}\n",
		"{\"ok\":truex}\n",
		"{\"ok\":tru}\n",
		"{\"ok\":}\n",
		"{\"reason\":\"\\\"ok\\\":true\"}\n", /* "ok" inside a string value */
		"{\"a\":{\"ok\":true}}\n",          /* nested, not top-level */
		"{\"a\":[\"ok\",true]}\n",
		"{\"ok\":false,\"ok\":true}\n",     /* duplicate: first wins, fails closed */
		"\n",
		"",                                 /* closed without a reply */
		/* Balanced, but not JSON: every one must be refused. */
		"{\"x\":1 \"ok\":true}\n",           /* missing comma */
		"{\"x\" \"ok\":true}\n",             /* key without a value */
		"{\"ok\":true \"reason\":\"no_match\"}\n",
		"{\"ok\" true}\n",                   /* missing colon */
		"{\"ok\"::true}\n",
		"{\"ok\":true,}\n",                  /* trailing comma */
		"{,\"ok\":true}\n",                  /* leading comma */
		"{\"ok\":true,,\"x\":1}\n",
		"{ok:true}\n",                       /* unquoted key */
		"{\"ok\":true,\"x\":[1,,2]}\n",
		"{\"ok\":true,\"x\":[1 2]}\n",
		"{\"ok\":true,\"x\":{\"a\"}}\n",
		"{\"ok\":true,\"x\":01}\n",          /* leading zero */
		"{\"ok\":true,\"x\":1.}\n",
		"{\"ok\":true,\"x\":.5}\n",
		"{\"ok\":true,\"x\":1e}\n",
		"{\"ok\":true,\"x\":-}\n",
		"{\"ok\":true,\"x\":NaN}\n",
		"{\"ok\":true,\"x\":nul}\n",
		"{\"ok\":true,\"x\":'y'}\n",
		"{\"ok\":true} x\n",
	};

	for (size_t i = 0; i < sizeof(cases) / sizeof(cases[0]); i++) {
		struct result r = reply(cases[i]);
		CHECK(r.rc != PAM_SUCCESS, "malformed reply %s authenticated", cases[i]);
		CHECK(r.rc == PAM_AUTH_ERR, "malformed reply %s -> %d, want AUTH_ERR", cases[i], r.rc);
	}

	/* Nesting right up to the validator's ceiling is fine... */
	memset(deep, 0, sizeof(deep));
	strcat(deep, "{\"ok\":true,\"x\":");
	for (int i = 1; i < IRIS_MAX_JSON_DEPTH; i++)
		strcat(deep, "[");
	for (int i = 1; i < IRIS_MAX_JSON_DEPTH; i++)
		strcat(deep, "]");
	strcat(deep, "}\n");
	CHECK(reply(deep).rc == PAM_SUCCESS, "reply at the depth ceiling refused");

	/* ...one level deeper is refused. */
	memset(deep, 0, sizeof(deep));
	strcat(deep, "{\"ok\":true,\"x\":");
	for (int i = 0; i < IRIS_MAX_JSON_DEPTH; i++)
		strcat(deep, "[");
	for (int i = 0; i < IRIS_MAX_JSON_DEPTH; i++)
		strcat(deep, "]");
	strcat(deep, "}\n");
	CHECK(reply(deep).rc == PAM_AUTH_ERR, "over-deep reply accepted");

	/* A reply that fills the buffer without a newline. */
	memset(big, ' ', sizeof(big));
	big[0] = '{';
	big[sizeof(big) - 1] = '\0';
	CHECK(reply(big).rc == PAM_AUTH_ERR, "oversized reply accepted");

	/* Progress lines are skipped, but only up to the line budget. */
	for (int i = 0; i < IRIS_MAX_REPLY_LINES - 1; i++)
		strcat(lines, "{\"progress\":0.5}\n");
	strcat(lines, "{\"ok\":true}\n");
	CHECK(reply(lines).rc == PAM_SUCCESS, "reply after %d progress lines refused",
	      IRIS_MAX_REPLY_LINES - 1);
	lines[0] = '\0';
	for (int i = 0; i < IRIS_MAX_REPLY_LINES; i++)
		strcat(lines, "{\"progress\":0.5}\n");
	strcat(lines, "{\"ok\":true}\n");
	CHECK(reply(lines).rc == PAM_AUTH_ERR, "line budget not enforced");
}

static void test_daemon_unavailable(void)
{
	pam_handle_t p = {0};
	const char *argv[] = {"timeout=2"};
	long start;
	int rc;

	/* No socket at all. */
	unlink(IRIS_SOCKET_PATH);
	prompts[0] = '\0';
	start = monotonic_ms();
	rc = pam_sm_authenticate(&p, 0, 1, argv);
	CHECK(rc == PAM_AUTHINFO_UNAVAIL, "no daemon -> %d", rc);
	CHECK(monotonic_ms() - start < 500, "no daemon took %ld ms", monotonic_ms() - start);
	/* Connect first, announce second: no "look at the camera" for nothing. */
	CHECK(prompts[0] == '\0', "prompted with no daemon: %s", prompts);

	/* A stale socket file nobody listens on. */
	int s = bind_server(1);
	close(s);
	rc = pam_sm_authenticate(&p, PAM_SILENT, 1, argv);
	CHECK(rc == PAM_AUTHINFO_UNAVAIL, "stale socket -> %d", rc);
	unlink(IRIS_SOCKET_PATH);
}

static void test_deadlines(void)
{
	const char *argv[] = {"timeout=1"};
	struct { const char *name; struct script sc; } cases[] = {
		{"silent daemon", {.hold_ms = 5000}},
		{"never accepts", {.no_accept = true, .hold_ms = 5000}},
		{"dribbles bytes", {.reply = "{\"ok\":true,\"pad\":\"xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\"}\n",
				    .drip_ms = 100, .hold_ms = 5000}},
		{"full backlog", {.no_accept = true, .hold_ms = 5000, .backlog = 1, .prefill = 4}},
	};

	for (size_t i = 0; i < sizeof(cases) / sizeof(cases[0]); i++) {
		struct result r = run(&cases[i].sc, 1, argv, PAM_SILENT);
		CHECK(r.rc != PAM_SUCCESS, "%s authenticated", cases[i].name);
		CHECK(r.elapsed_ms < 1500, "%s held the login for %ld ms", cases[i].name, r.elapsed_ms);
	}
}

static void test_daemon_hangs_up_early(void)
{
	/*
	 * The daemon accepts and hangs up at once; the "look at the camera"
	 * prompt (between connect and send) waits until it has, so send() hits
	 * a closed socket. Without MSG_NOSIGNAL that raises SIGPIPE, whose
	 * default action would kill the host (sudo, gdm) and this test with it.
	 */
	struct script sc = {.no_read = true};
	const char *argv[] = {"timeout=2"};
	struct sigaction dfl = {.sa_handler = SIG_DFL}, old;

	sigaction(SIGPIPE, &dfl, &old);
	prompt_delay_ms = 200;
	struct result r = run(&sc, 1, argv, 0);
	prompt_delay_ms = 0;
	sigaction(SIGPIPE, &old, NULL);
	CHECK(r.rc == PAM_AUTH_ERR, "early hang-up -> %d", r.rc);
}

static void test_request_format(void)
{
	struct { const char *arg; const char *want; } cases[] = {
		{NULL, "{\"op\":\"auth\",\"user\":\"alice\",\"timeout\":7.000}\n"},
		{"timeout=2.5", "{\"op\":\"auth\",\"user\":\"alice\",\"timeout\":1.500}\n"},
		{"timeout=1", "{\"op\":\"auth\",\"user\":\"alice\",\"timeout\":0.500}\n"},
		{"timeout=60", "{\"op\":\"auth\",\"user\":\"alice\",\"timeout\":59.000}\n"},
	};
	struct script sc = {.reply = "{\"ok\":false}\n"};

	for (size_t i = 0; i < sizeof(cases) / sizeof(cases[0]); i++) {
		const char *argv[] = {cases[i].arg};
		struct result r = run(&sc, cases[i].arg ? 1 : 0, argv, PAM_SILENT);
		CHECK(strcmp(r.request, cases[i].want) == 0, "request %s, want %s", r.request, cases[i].want);
	}

	current_user = "zo\xc3\xab \"q\" \\b";
	struct result r = run(&sc, 0, NULL, PAM_SILENT);
	CHECK(strstr(r.request, "\"user\":\"zo\xc3\xab \\\"q\\\" \\\\b\"") != NULL,
	      "username not escaped: %s", r.request);
	current_user = "alice";
}

static void test_usernames_are_checked_before_any_io(void)
{
	char longest[IRIS_MAX_USERNAME + 1];
	char too_long[IRIS_MAX_USERNAME + 2];
	const char *bad[] = {"a\nb", "a\tb", "a\x1b[31mb", "a\x7f"};
	pam_handle_t p = {0};

	unlink(IRIS_SOCKET_PATH); /* touching the socket would give AUTHINFO_UNAVAIL */
	for (size_t i = 0; i < sizeof(bad) / sizeof(bad[0]); i++) {
		current_user = bad[i];
		CHECK(pam_sm_authenticate(&p, PAM_SILENT, 0, NULL) == PAM_AUTH_ERR,
		      "control characters in username %zu not refused", i);
	}
	memset(too_long, 'a', sizeof(too_long) - 1);
	too_long[sizeof(too_long) - 1] = '\0';
	current_user = too_long;
	CHECK(pam_sm_authenticate(&p, PAM_SILENT, 0, NULL) == PAM_AUTH_ERR, "257-byte username not refused");
	current_user = "";
	CHECK(pam_sm_authenticate(&p, PAM_SILENT, 0, NULL) == PAM_AUTH_ERR, "empty username not refused");

	memset(longest, 'a', sizeof(longest) - 1);
	longest[sizeof(longest) - 1] = '\0';
	current_user = longest;
	CHECK(pam_sm_authenticate(&p, PAM_SILENT, 0, NULL) == PAM_AUTHINFO_UNAVAIL,
	      "256-byte username was refused instead of sent");
	current_user = "alice";
}

static void test_user_messages_and_logs(void)
{
	struct script sc = {.reply = "{\"ok\":false,\"reason\":\"no_match\"}\n"};
	struct result r = run(&sc, 0, NULL, 0);

	CHECK(strcmp(prompts, "Iris: look at the infrared camera...|Iris: face not recognised.|") == 0,
	      "prompts: %s", prompts);

	/* Normal states are not announced as errors. */
	sc.reply = "{\"ok\":false,\"reason\":\"not_enrolled\"}\n";
	run(&sc, 0, NULL, 0);
	CHECK(strcmp(prompts, "Iris: look at the infrared camera...|") == 0, "not_enrolled prompts: %s", prompts);

	/* quiet and PAM_SILENT both silence the conversation. */
	const char *quiet[] = {"quiet"};
	sc.reply = "{\"ok\":false,\"reason\":\"no_match\"}\n";
	run(&sc, 1, quiet, 0);
	CHECK(prompts[0] == '\0', "quiet still prompted: %s", prompts);
	run(&sc, 0, NULL, PAM_SILENT);
	CHECK(prompts[0] == '\0', "PAM_SILENT still prompted: %s", prompts);

	/* The daemon's text never reaches the terminal or the log verbatim. */
	sc.reply = "{\"ok\":false,\"reason\":\"no\\nmatch\\u001b[2J\"}\n";
	r = run(&sc, 0, NULL, 0);
	CHECK(r.rc == PAM_AUTH_ERR, "odd reason -> %d", r.rc);
	CHECK(strchr(last_log, '\n') == NULL && strchr(last_log, '\x1b') == NULL,
	      "unsanitised log line: %s", last_log);
	CHECK(strstr(last_log, "reason=no.match") != NULL, "log line: %s", last_log);
	CHECK(strcmp(prompts, "Iris: look at the infrared camera...|") == 0, "odd reason prompts: %s", prompts);
}

static void test_arguments(void)
{
	/* Literal values: the documented contract is 8 s default, clamped to [1, 60]. */
	struct { const char *arg; long want; } cases[] = {
		{"timeout=2.5", 2500}, {"timeout=0", 1000}, {"timeout=-4", 1000},
		{"timeout=999", 60000}, {"timeout=60", 60000}, {"timeout=1", 1000},
		{"timeout=", 8000}, {"timeout=abc", 8000}, {"timeout=5s", 8000},
		{"timeout=nan", 8000}, {"timeout=1e999", 8000}, /* ERANGE */
		{"bogus", 8000},
		/* out of range for a long: clamped in floating point, never cast */
		{"timeout=inf", 60000}, {"timeout=1e300", 60000}, {"timeout=9e18", 60000},
		{"timeout=-inf", 1000}, {"timeout=-1e300", 1000},
	};
	struct iris_opts o;

	for (size_t i = 0; i < sizeof(cases) / sizeof(cases[0]); i++) {
		const char *argv[] = {cases[i].arg};
		parse_args(NULL, 1, argv, &o);
		CHECK(o.timeout_ms == cases[i].want, "%s -> %ld ms, want %ld", cases[i].arg, o.timeout_ms,
		      cases[i].want);
	}
	const char *both[] = {"debug", "quiet"};
	parse_args(NULL, 2, both, &o);
	CHECK(o.debug && o.quiet, "debug/quiet not parsed");
}

int main(void)
{
	pam_handle_t p = {0};

	/* Watchdog: the module must never hang, so a hang here is a failure. */
	alarm(30);

	test_verdicts();
	test_malformed_replies_never_authenticate();
	test_daemon_unavailable();
	test_deadlines();
	test_daemon_hangs_up_early();
	test_request_format();
	test_usernames_are_checked_before_any_io();
	test_user_messages_and_logs();
	test_arguments();
	CHECK(pam_sm_setcred(&p, 0, 0, NULL) == PAM_IGNORE, "setcred must be PAM_IGNORE");

	if (failures) {
		fprintf(stderr, "%d PAM protocol check(s) failed\n", failures);
		return 1;
	}
	puts("PASS: pam_iris verdicts, malformed replies, deadlines, requests, usernames and messages");
	return 0;
}
