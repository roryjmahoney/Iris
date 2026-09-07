#include <security/pam_appl.h>

#include <stdio.h>
#include <string.h>

static int reject_conversation(int count, const struct pam_message **messages,
			       struct pam_response **responses, void *data)
{
	(void)count;
	(void)messages;
	(void)responses;
	(void)data;
	return PAM_CONV_ERR;
}

int main(int argc, char **argv)
{
	const struct pam_conv conversation = {reject_conversation, NULL};
	pam_handle_t *handle = NULL;
	int expected;
	int result;

	if (argc != 4) {
		fprintf(stderr, "usage: %s SERVICE CONFDIR success|auth-error\n", argv[0]);
		return 64;
	}
	if (strcmp(argv[3], "success") == 0)
		expected = PAM_SUCCESS;
	else if (strcmp(argv[3], "auth-error") == 0)
		expected = PAM_AUTH_ERR;
	else {
		fprintf(stderr, "unknown expected result: %s\n", argv[3]);
		return 64;
	}

	/* The control character makes pam_iris fail before any socket or camera I/O. */
	result = pam_start_confdir(argv[1], "invalid\nuser", &conversation, argv[2], &handle);
	if (result == PAM_SUCCESS)
		result = pam_authenticate(handle, PAM_SILENT);

	if (handle != NULL) {
		int end_result = pam_end(handle, result);

		if (end_result != PAM_SUCCESS) {
			fprintf(stderr, "pam_end returned %d\n", end_result);
			return 1;
		}
	}

	if (result != expected) {
		fprintf(stderr, "%s returned %d, expected %d\n", argv[1], result, expected);
		return 1;
	}
	printf("%s: PAM result %d\n", argv[1], result);
	return 0;
}
