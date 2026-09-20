# ============================================================================
#  Iris — PAM module build
# ============================================================================
#
#  pam_iris.so is loaded into sudo, su, login and the GNOME greeter, so it is
#  built with warnings on and optimisation at -O2 and nothing else: no external
#  libraries beyond libpam itself, no generated code, no link-time surprises.
#
#      make            build pam_iris.so in this directory
#      make check      compile-only, extra-strict warnings (no output file)
#      sudo make install
#      sudo make uninstall
#      make test       print the pamtester recipe for a SAFE isolated stack
#      make clean
#
#  The installer script owns /etc and /usr in a normal deployment; the install
#  target here exists for development and for that script to call.
#
#  SAFETY: after installing, the module must be stacked as
#
#      auth    [success=done default=ignore]    pam_iris.so
#
#  ahead of pam_unix, and NEVER as the only auth module in a stack.  Any other
#  control value (required/requisite) turns a camera failure into a permanent
#  lockout with no password fallback.  See the header of pam_iris.c.
# ============================================================================

CC      ?= gcc
CFLAGS  ?= -Wall -Wextra -O2
LDLIBS  ?= -lpam

# Flags the module CANNOT be correctly built without.  They are deliberately
# NOT part of the overridable CFLAGS: `CFLAGS ?=` is skipped entirely when the
# environment already exports CFLAGS (a distro build, or just a developer with
# it set in their shell), and `make CFLAGS=-O0` overrides it outright.  Either
# would silently drop -fPIC/-shared and produce a .so that is broken or that
# fails only at dlopen() time — inside a live auth stack.
#
#   -shared/-fPIC   it is a dlopen()ed module, not a program
#   --no-undefined  resolve every symbol NOW, at link time.  Without it an
#                   undefined symbol links fine and surfaces as "cannot open
#                   shared object" the first time someone runs sudo.
#   -z relro -z now  full RELRO: no writable GOT in a module loaded by every
#                   setuid program on the machine.
#   _FORTIFY_SOURCE requires an optimising build to do anything, hence -O2
#                   staying in the default CFLAGS above.
REQUIRED_CFLAGS  := -fPIC -shared -D_FORTIFY_SOURCE=2 -fstack-protector-strong
REQUIRED_LDFLAGS := -Wl,--no-undefined -Wl,-z,relro -Wl,-z,now

# Extra-strict pass used by `make check`; kept separate so the shipped build
# command stays exactly the one documented in the header above.
#
# -Werror is deliberate: without it the compiler prints warnings, exits 0 and
# the target still announces "no warnings", which is worse than no check at all.
#
# -Wpedantic is deliberately NOT in this list.  Every pam_syslog() call here
# uses the "%m" conversion (the errno string), which is a documented glibc
# syslog(3) extension that pam_syslog implements — ISO C does not know it, so
# -Wpedantic reports ten false positives and drowns the real findings.
CHECKFLAGS ?= -fsyntax-only -Werror -Wall -Wextra -Wshadow -Wconversion \
              -Wsign-conversion -Wcast-qual -Wformat=2 -Wstrict-prototypes \
              -Wmissing-prototypes -Wvla

TARGET  := pam_iris.so pam_iris_keyring.so
SOURCES := pam_iris.c pam_iris_keyring.c

# Multiarch security directory for x86_64 Debian/Ubuntu.  Override for another
# architecture, e.g.  make install SECDIR=/usr/lib/aarch64-linux-gnu/security
SECDIR  ?= /usr/lib/x86_64-linux-gnu/security
DESTDIR ?=

# The PAM config snippet is NOT installed by this Makefile.  Editing
# /etc/pam.d is the installer's job precisely because a bad edit there is the
# one failure mode that cannot be undone from a locked-out console.
PAMD_HINT := /etc/pam.d/common-auth

.PHONY: all check install uninstall test clean

all: $(TARGET)

$(TARGET): %.so: %.c iris_pam_state.h
	$(CC) $(CFLAGS) $(REQUIRED_CFLAGS) $(LDFLAGS) $(REQUIRED_LDFLAGS) \
	    -o $@ $< $(LDLIBS)
	@# A PAM module that dlopen()s with an unresolved symbol is discovered
	@# at the worst possible moment.  Prove it is clean here instead.
	@if command -v ldd >/dev/null 2>&1; then \
	    if ldd -r $@ 2>&1 | grep -q "undefined symbol"; then \
	        echo "ERROR: $@ has undefined symbols; refusing to ship it" >&2; \
	        ldd -r $@ 2>&1 | grep "undefined symbol" >&2; \
	        rm -f $@; exit 1; \
	    fi; \
	    echo "$@: no undefined symbols"; \
	fi

check:
	$(CC) $(CHECKFLAGS) $(SOURCES)
	@echo "check: no warnings"

install: $(TARGET)
	@test "$$(id -u)" = "0" || { \
	    echo "install: must be run as root (try: sudo make install)" >&2; \
	    exit 1; \
	}
	install -d -m 0755 $(DESTDIR)$(SECDIR)
	install -m 0644 -o root -g root $(TARGET) $(DESTDIR)$(SECDIR)/
	@echo
	@echo "Installed $(DESTDIR)$(SECDIR)/$(TARGET)"
	@echo "It is NOT active until it is stacked.  Add to $(PAMD_HINT),"
	@echo "above the pam_unix line:"
	@echo
	@echo "    auth    [success=done default=ignore]    pam_iris.so"
	@echo
	@echo "Keep a root shell open and verify password login still works"
	@echo "before closing it."

uninstall:
	@test "$$(id -u)" = "0" || { \
	    echo "uninstall: must be run as root (try: sudo make uninstall)" >&2; \
	    exit 1; \
	}
	rm -f $(addprefix $(DESTDIR)$(SECDIR)/,$(TARGET))
	@echo "Removed $(DESTDIR)$(SECDIR)/$(TARGET)."
	@echo "Remove the pam_iris.so line from $(PAMD_HINT) as well:"
	@echo "PAM logs 'module unknown' and ignores it, but leaving dead"
	@echo "entries in an auth stack is how the next outage starts."

# Exercise the module WITHOUT touching a stack that can lock you out.
# pamtester drives a named service directly, so a dedicated throwaway service
# file is the only safe way to test this.
test: $(TARGET)
	@echo "Test the module in isolation with pamtester (apt install pamtester):"
	@echo
	@echo "  sudo make install"
	@echo "  sudo tee /etc/pam.d/iris-test >/dev/null <<'EOF'"
	@echo "  auth    [success=done default=ignore]    pam_iris.so debug timeout=8"
	@echo "  auth    required                         pam_deny.so"
	@echo "  account required                         pam_permit.so"
	@echo "  EOF"
	@echo
	@echo "  # irisd must be running; the socket is root-only, so run as root:"
	@echo "  sudo pamtester -v iris-test $$USER authenticate"
	@echo "  sudo journalctl -t pamtester -n 30 --no-pager"
	@echo
	@echo "Expect: PAM_SUCCESS on a match; on any failure pam_deny.so denies,"
	@echo "which proves 'default=ignore' really did fall through."
	@echo
	@echo "Timeout behaviour with irisd stopped (must return promptly,"
	@echo "not hang):"
	@echo "  sudo systemctl stop irisd"
	@echo "  time sudo pamtester -v iris-test $$USER authenticate"
	@echo
	@echo "Clean up:  sudo rm /etc/pam.d/iris-test"

clean:
	rm -f $(TARGET)
