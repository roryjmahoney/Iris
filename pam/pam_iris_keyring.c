/* Optional GDM session adapter. Authentication decisions belong to the stack;
 * this module never prompts or grants/denies login. Root is trusted. The face
 * marker is PAM-handle data from pam_iris, never an environment or disk flag.
 * gkr_system_authtok is GNOME Keyring's version-sensitive private PAM contract.
 */
#define _GNU_SOURCE
#include <security/pam_modules.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>
#include "iris_pam_state.h"
#ifndef IRIS_KEYRING_TEST
#define IRIS_KEYRING_HELPER "/usr/lib/iris/iris/keyring.py"
#endif
#define SECRET_MAX 4096
#define USER_MAX 256
struct capture { char user[USER_MAX + 1]; char service[32]; char secret[SECRET_MAX + 1]; };
static void free_capture(pam_handle_t *pamh, void *data, int status)
{
    (void)pamh; (void)status;
    if (data) { explicit_bzero(data, sizeof(struct capture)); free(data); }
}
static int64_t milliseconds(void)
{
    struct timespec ts;
    if (clock_gettime(CLOCK_MONOTONIC, &ts)) return -1;
    return (int64_t)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}
static bool identity(pam_handle_t *pamh, const char **user)
{
    const void *u = NULL, *s = NULL;
    if (pam_get_item(pamh, PAM_USER, &u) || pam_get_item(pamh, PAM_SERVICE, &s)
        || !u || !s || strcmp(s, "gdm-password")) return false;
    size_t n = strnlen(u, USER_MAX + 1);
    if (!n || n > USER_MAX) return false;
    *user = u;
    for (size_t i = 0; i < n; ++i)
        if ((unsigned char)(*user)[i] < 32 || (*user)[i] == 127) return false;
    return true;
}
/* Pre-fill the private input pipe before fork: <= PIPE_BUF, nonblocking and no
 * SIGPIPE exposure (the reader is still held here). Parent reads nonblocking;
 * child exit and EOF share one total monotonic five-second deadline. */
static bool helper(const char *op, const char *user, const char *input, char *output)
{
    int in[2] = {-1, -1}, out[2] = {-1, -1};
    pid_t pid = -1;
    bool ok = false, eof = false, exited = false;
    int status = 0;
    size_t used = 0;
    int64_t start = milliseconds(), deadline = start + 5000;
    if (start < 0 || pipe2(in, O_CLOEXEC | O_NONBLOCK) || pipe2(out, O_CLOEXEC | O_NONBLOCK)) goto done;
    if (input) {
        size_t n = strlen(input);
        if (n > SECRET_MAX || write(in[1], input, n) != (ssize_t)n) goto done;
    }
    close(in[1]); in[1] = -1;
    pid = fork();
    if (pid < 0) goto done;
    if (pid == 0) {
        if (setpgid(0, 0)) _exit(126);
        /* Move above stdio first, including when the PAM host closed stdio. */
        int a = fcntl(in[0], F_DUPFD_CLOEXEC, 3);
        int b = fcntl(out[1], F_DUPFD_CLOEXEC, 3);
        int nullraw = open("/dev/null", O_WRONLY | O_CLOEXEC);
        int nullfd = nullraw < 0 ? -1 : fcntl(nullraw, F_DUPFD_CLOEXEC, 3);
        if (a < 0 || b < 0 || nullfd < 0 || dup2(a, 0) < 0 || dup2(b, 1) < 0 || dup2(nullfd, 2) < 0) _exit(126);
        if (fcntl(0, F_SETFL, 0) || fcntl(1, F_SETFL, 0)) _exit(126);
        if (close_range(3, ~0U, 0)) _exit(126);
        char *const env[] = { "PATH=/usr/bin:/bin", "LANG=C", NULL };
        /* execl-style arguments without putting the password in argv/env. */
        union { const char *c; char *m; } operation = {.c = op}, name = {.c = user};
        char *const args[] = { "/usr/bin/python3", "-I", "-B", IRIS_KEYRING_HELPER, operation.m, name.m, NULL };
        execve(args[0], args, env);
        _exit(127);
    }
    (void)setpgid(pid, pid);
    close(in[0]); in[0] = -1;
    close(out[1]); out[1] = -1;
    while (!eof || !exited) {
        int64_t now = milliseconds();
        if (now < 0 || now >= deadline) goto done;
        if (!eof) {
            char buffer[512];
            ssize_t n = read(out[0], buffer, sizeof(buffer));
            if (n > 0) {
                if ((size_t)n > SECRET_MAX - used || memchr(buffer, 0, (size_t)n)) {
                    explicit_bzero(buffer, sizeof(buffer)); goto done;
                }
                memcpy(output + used, buffer, (size_t)n); used += (size_t)n;
                explicit_bzero(buffer, sizeof(buffer));
            } else if (n == 0) eof = true;
            else if (errno != EAGAIN && errno != EINTR) goto done;
        }
        if (!exited) {
            pid_t r = waitpid(pid, &status, WNOHANG);
            if (r == pid) exited = true;
            else if (r < 0 && errno != EINTR) goto done;
        }
        if (!eof || !exited) {
            struct pollfd p = { .fd = eof ? -1 : out[0], .events = POLLIN };
            (void)poll(&p, 1, 10);
        }
    }
    output[used] = 0;
    ok = WIFEXITED(status) && WEXITSTATUS(status) == 0 &&
        (strcmp(op, "capture") == 0 ? used == 0 : used > 0);
done:
    if (pid > 0) {
        /* Kill descendants too, including ones retaining pipe ends after exit. */
        (void)kill(-pid, SIGKILL);
        if (!exited) { (void)kill(pid, SIGKILL); while (waitpid(pid, NULL, 0) < 0 && errno == EINTR) {} }
    }
    for (int i = 0; i < 2; ++i) { if (in[i] >= 0) close(in[i]); if (out[i] >= 0) close(out[i]); }
    if (!ok) explicit_bzero(output, SECRET_MAX + 1);
    return ok;
}
int pam_sm_authenticate(pam_handle_t *pamh, int flags, int argc, const char **argv)
{
    const char *user = NULL;
    const void *token = NULL;
    (void)flags; (void)argc; (void)argv;
    iris_clear_data(pamh, IRIS_CAPTURE_DATA);
    if (!identity(pamh, &user) || pam_get_item(pamh, PAM_AUTHTOK, &token) || !token) return PAM_IGNORE;
    size_t n = strnlen(token, SECRET_MAX + 1);
    if (!n || n > SECRET_MAX) return PAM_IGNORE;
    struct capture *c = calloc(1, sizeof(*c));
    if (!c) return PAM_IGNORE;
    memcpy(c->user, user, strlen(user) + 1);
    memcpy(c->service, "gdm-password", sizeof("gdm-password"));
    memcpy(c->secret, token, n + 1);
    if (pam_set_data(pamh, IRIS_CAPTURE_DATA, c, free_capture)) free_capture(pamh, c, 0);
    return PAM_IGNORE;
}
int pam_sm_open_session(pam_handle_t *pamh, int flags, int argc, const char **argv)
{
    const char *user = NULL;
    const void *marker = NULL, *captured = NULL;
    char output[SECRET_MAX + 1] = {0};
    struct capture copy = {0};
    bool face = false, valid = identity(pamh, &user);
    (void)flags; (void)argc; (void)argv;
    if (valid && pam_get_data(pamh, IRIS_FACE_DATA, &marker) == PAM_SUCCESS && marker)
        face = strcmp(marker, user) == 0;
    if (valid && pam_get_data(pamh, IRIS_CAPTURE_DATA, &captured) == PAM_SUCCESS && captured) {
        const struct capture *c = captured;
        if (!strcmp(c->user, user) && !strcmp(c->service, "gdm-password")) memcpy(&copy, c, sizeof(copy));
    }
    iris_clear_data(pamh, IRIS_FACE_DATA);
    iris_clear_data(pamh, IRIS_CAPTURE_DATA);
#ifndef IRIS_KEYRING_TEST
    valid = valid && geteuid() == 0;
#endif
    if (valid && face && helper("unlock", user, NULL, output)) {
        char *secret = strdup(output);
        if (secret && pam_set_data(pamh, "gkr_system_authtok", secret, iris_free_string)) iris_free_string(pamh, secret, 0);
    } else if (valid && !face && copy.user[0]) {
        (void)helper("capture", user, copy.secret, output);
    }
    explicit_bzero(&copy, sizeof(copy));
    explicit_bzero(output, sizeof(output));
    return PAM_IGNORE;
}
int pam_sm_setcred(pam_handle_t *pamh, int flags, int argc, const char **argv)
{ (void)pamh; (void)flags; (void)argc; (void)argv; return PAM_IGNORE; }
int pam_sm_close_session(pam_handle_t *pamh, int flags, int argc, const char **argv)
{ (void)flags; (void)argc; (void)argv; iris_clear_data(pamh, IRIS_FACE_DATA); iris_clear_data(pamh, IRIS_CAPTURE_DATA); return PAM_IGNORE; }
