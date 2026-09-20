/* Real socket protocol + synthetic PAM handle; no live PAM or daemon changes. */
#define _GNU_SOURCE
#include <assert.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <security/pam_modules.h>
#include <security/pam_ext.h>
struct pam_handle { char *marker; char *capture; };
static const char *current_user = "alice";
static const char *current_service = "gdm-password";
static int fail_data;
int pam_get_data(const pam_handle_t *p, const char *key, const void **v)
{ *v = strstr(key, "face") ? p->marker : p->capture; return PAM_SUCCESS; }
int pam_set_data(pam_handle_t *p, const char *key, void *v, void (*cleanup)(pam_handle_t *, void *, int))
{
    (void)cleanup;
    if (fail_data) return PAM_BUF_ERR;
    char **slot = strstr(key, "face") ? &p->marker : &p->capture;
    free(*slot); *slot = v; return PAM_SUCCESS;
}
int pam_get_item(const pam_handle_t *p, int item, const void **v)
{ (void)p; *v = item == PAM_SERVICE ? current_service : current_user; return PAM_SUCCESS; }
int pam_get_user(pam_handle_t *p, const char **user, const char *prompt)
{ (void)p; (void)prompt; *user = current_user; return PAM_SUCCESS; }
void pam_syslog(const pam_handle_t *p, int priority, const char *fmt, ...)
{ (void)p; (void)priority; (void)fmt; }
#include "../pam/pam_iris.c"
static int attempt(pam_handle_t *p, const char *reply)
{
    int server = socket(AF_UNIX, SOCK_STREAM, 0); assert(server >= 0);
    struct sockaddr_un a = {.sun_family = AF_UNIX};
    assert(strlen(IRIS_SOCKET_PATH) < sizeof(a.sun_path));
    memcpy(a.sun_path, IRIS_SOCKET_PATH, strlen(IRIS_SOCKET_PATH)+1);
    assert(!bind(server, (struct sockaddr *)&a, sizeof(a))); assert(!listen(server, 1));
    pid_t child = fork(); assert(child >= 0);
    if (!child) {
        int c = accept(server, NULL, NULL); char buf[1024];
        if (c < 0 || read(c, buf, sizeof(buf)) <= 0) _exit(1);
        if (write(c, reply, strlen(reply)) != (ssize_t)strlen(reply)) _exit(1);
        close(c); close(server); _exit(0);
    }
    int r = pam_sm_authenticate(p, PAM_SILENT, 0, NULL), status;
    assert(waitpid(child, &status, 0) == child && status == 0);
    close(server); unlink(IRIS_SOCKET_PATH); return r;
}
int main(void)
{
    pam_handle_t p = {0};
    assert(attempt(&p, "{\"ok\":true}\n") == PAM_SUCCESS);
    assert(p.marker && !strcmp(p.marker, "alice"));
    p.capture = strdup("alice");
    assert(attempt(&p, "{\"ok\":false}\n") == PAM_AUTH_ERR);
    assert(!p.marker && !p.capture);
    current_service = "sudo";
    assert(attempt(&p, "{\"ok\":true}\n") == PAM_SUCCESS); assert(!p.marker);
    current_service = "gdm-password"; fail_data = 1;
    p.marker = strdup("alice"); p.capture = strdup("alice");
    assert(attempt(&p, "{\"ok\":true}\n") == PAM_SUCCESS);
    assert(p.marker[0] == 0 && p.capture[0] == 0);
    current_user = "invalid\nuser";
    assert(pam_sm_authenticate(&p, PAM_SILENT, 0, NULL) == PAM_AUTH_ERR);
    assert(pam_sm_setcred(&p, 0, 0, NULL) == PAM_IGNORE);
    free(p.marker); free(p.capture);
    puts("PASS: real face protocol markers, repeated attempts, service binding and PAM data failures");
}
