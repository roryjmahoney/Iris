#ifndef IRIS_PAM_STATE_H
#define IRIS_PAM_STATE_H
#include <security/pam_modules.h>
#include <stdlib.h>
#include <string.h>
#define IRIS_FACE_DATA "iris.face_authenticated.v1"
#define IRIS_CAPTURE_DATA "iris.keyring.capture.v1"
/* PAM owns mutable allocations despite its const-qualified lookup API. Wipe
 * before removing so even a failed pam_set_data cannot leave usable state. */
static inline void iris_clear_data(pam_handle_t *pamh, const char *key)
{
    union { const void *read; void *write; } value = { .read = NULL };
    if (pam_get_data(pamh, key, &value.read) == PAM_SUCCESS && value.read)
        ((char *)value.write)[0] = '\0';
    (void)pam_set_data(pamh, key, NULL, NULL);
}
static inline void iris_free_string(pam_handle_t *pamh, void *data, int status)
{
    (void)pamh; (void)status;
    if (data) { explicit_bzero(data, strlen(data)); free(data); }
}
#endif
