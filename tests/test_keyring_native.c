#define _GNU_SOURCE
#include <assert.h>
#include <stdio.h>
#include <security/pam_modules.h>
struct slot { const char *key; void *data; void (*cleanup)(pam_handle_t *, void *, int); };
struct pam_handle { const char *user, *service, *token; struct slot slots[8]; };
int pam_get_item(const pam_handle_t *p, int item, const void **v)
{
    *v = item == PAM_USER ? p->user : item == PAM_SERVICE ? p->service : item == PAM_AUTHTOK ? p->token : NULL;
    return PAM_SUCCESS;
}
#include <string.h>
int pam_get_data(const pam_handle_t *p, const char *key, const void **v)
{
    *v = NULL;
    for (int i = 0; i < 8; ++i) if (p->slots[i].key && !strcmp(key, p->slots[i].key)) { *v = p->slots[i].data; return PAM_SUCCESS; }
    return PAM_NO_MODULE_DATA;
}
int pam_set_data(pam_handle_t *p, const char *key, void *v, void (*cleanup)(pam_handle_t *, void *, int))
{
    for (int i = 0; i < 8; ++i) if (!p->slots[i].key || !strcmp(key, p->slots[i].key)) {
        if (p->slots[i].cleanup) p->slots[i].cleanup(p, p->slots[i].data, 0);
        p->slots[i] = (struct slot){key, v, cleanup}; return PAM_SUCCESS;
    }
    return PAM_BUF_ERR;
}
#include "../pam/pam_iris_keyring.c"
static void clear(pam_handle_t *p)
{
    for (int i = 0; i < 8; ++i) if (p->slots[i].cleanup) p->slots[i].cleanup(p, p->slots[i].data, 0);
    memset(p->slots, 0, sizeof(p->slots));
}
static void face(pam_handle_t *p, const char *user)
{ assert(!pam_set_data(p, IRIS_FACE_DATA, strdup(user), iris_free_string)); }
static const char *gkr(pam_handle_t *p)
{ const void *v = NULL; (void)pam_get_data(p, "gkr_system_authtok", &v); return v; }
static void session(pam_handle_t *p)
{ assert(pam_sm_open_session(p, 0, 0, NULL) == PAM_IGNORE); }
static int events(void)
{
    FILE *f = fopen(IRIS_KEYRING_HELPER ".events", "r");
    int n = 0, ch;
    if (!f) return 0;
    while ((ch = fgetc(f)) != EOF) if (ch == '\n') ++n;
    fclose(f); return n;
}
int main(void)
{
    pam_handle_t p = {.user="alice", .service="gdm-password", .token="synthetic-only"};
    session(&p); assert(!gkr(&p)); assert(events() == 0);
    assert(pam_sm_authenticate(&p, 0, 0, NULL) == PAM_IGNORE);
    assert(!gkr(&p)); assert(events() == 0); /* auth alone neither stores nor releases via helper */
    session(&p); assert(!gkr(&p)); assert(events() == 1);
    const void *v; pam_get_data(&p, IRIS_CAPTURE_DATA, &v); assert(!v);
    face(&p, "alice"); session(&p); assert(!strcmp(gkr(&p), "synthetic-only"));
    pam_set_data(&p, "gkr_system_authtok", NULL, NULL);
    session(&p); assert(!gkr(&p)); assert(events() == 2); /* consumed face marker */
    face(&p, "bob"); session(&p); assert(!gkr(&p));
    face(&p, "alice"); p.service="sudo"; session(&p); assert(!gkr(&p));
    p.service="gdm-password"; session(&p); assert(!gkr(&p)); assert(events() == 2);
    pam_sm_authenticate(&p, 0, 0, NULL); p.user="bob"; session(&p); assert(!gkr(&p));
    assert(events() == 2);
    p.user="alice"; pam_sm_authenticate(&p, 0, 0, NULL); p.token=NULL;
    pam_sm_authenticate(&p, 0, 0, NULL); pam_get_data(&p, IRIS_CAPTURE_DATA, &v); assert(!v);
    const char *bad[] = {"oversized", "nul", "failure", "timeout"};
    for (size_t i=0; i<sizeof(bad)/sizeof(bad[0]); ++i) {
        p.user=bad[i]; face(&p, p.user); int64_t t=milliseconds(); session(&p);
        assert(milliseconds()-t < 6500); assert(!gkr(&p));
    }
    char out[SECRET_MAX+1];
    assert(helper("capture", "alice", "synthetic-only", out));
    assert(!helper("capture", "alice", "wrong", out));
    clear(&p);
    puts("PASS: keyring PAM identity, one-shot state, bounds, timeout, capture and release");
    return 0;
}
