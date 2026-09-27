"""Terminal output: colour, glyph fallbacks and the shared console."""

from __future__ import annotations

import os
import sys
from typing import TextIO

from iris.cli.constants import EXIT_FAILURE


#: Typographic characters swapped for ASCII when the output encoding cannot
#: represent them.  This covers our own strings *and* text that arrives from
#: elsewhere — a pose hint from the daemon or an exception message from a
#: library can contain anything, and a UnicodeEncodeError raised while printing
#: an error message is a spectacularly unhelpful failure mode.
_ASCII_FALLBACKS = str.maketrans({
    "—": "-", "–": "-", "→": "->", "←": "<-", "…": "...",
    "·": "*", "✓": "OK", "✗": "X", "″": '"', "’": "'", "“": '"', "”": '"',
})

_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_RED = "\033[31m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_CYAN = "\033[36m"


class Console:
    """Stdout/stderr writer that knows what the terminal can render.

    Two independent capabilities are probed, because they fail independently:
    ANSI colour (a TTY that is not ``dumb``, with ``NO_COLOR`` honoured) and
    non-ASCII glyphs (whether the stream's encoding can represent them —
    ``LC_ALL=C`` gives ASCII-only, and writing ``✓`` there raises).
    """

    def __init__(self, stream: TextIO | None = None, err: TextIO | None = None) -> None:
        self.stream: TextIO = stream if stream is not None else sys.stdout
        self.err: TextIO = err if err is not None else sys.stderr
        self.color = False
        self.unicode = False
        self.tty = False
        self.configure()

    # -- setup ------------------------------------------------------------

    def configure(self, when: str = "auto") -> None:
        self.tty = _is_tty(self.stream)
        if when == "always":
            self.color = True
        elif when == "never":
            self.color = False
        else:
            # https://no-color.org: any value, including empty, disables colour.
            self.color = (
                self.tty
                and "NO_COLOR" not in os.environ
                and os.environ.get("TERM", "") != "dumb"
            )
        self.unicode = _encodable(self.stream, "✓✗█░→·") and _encodable(self.err, "✓✗█░→·")
        if not self.unicode:
            # Last line of defence: transliteration below handles the characters
            # we know about, but a stray glyph in third-party text must degrade
            # to "?" rather than aborting the command mid-sentence.
            for stream in (self.stream, self.err):
                try:
                    stream.reconfigure(errors="replace")  # type: ignore[union-attr]
                except (AttributeError, ValueError, OSError):
                    pass

    # -- painting ---------------------------------------------------------

    def paint(self, text: str, *codes: str) -> str:
        if not self.color or not codes:
            return text
        return "".join(codes) + text + _RESET

    def bold(self, text: str) -> str:
        return self.paint(text, _BOLD)

    def dim(self, text: str) -> str:
        return self.paint(text, _DIM)

    def red(self, text: str) -> str:
        return self.paint(text, _RED)

    def green(self, text: str) -> str:
        return self.paint(text, _GREEN)

    def yellow(self, text: str) -> str:
        return self.paint(text, _YELLOW)

    def cyan(self, text: str) -> str:
        return self.paint(text, _CYAN)

    # -- writing ----------------------------------------------------------

    def text(self, value: str) -> str:
        """Transliterate typography the output encoding cannot represent."""
        return value if self.unicode else value.translate(_ASCII_FALLBACKS)

    def print(self, text: str = "") -> None:
        print(self.text(text), file=self.stream)

    def write(self, text: str) -> None:
        """Write without a newline and flush — used by the progress bar."""
        self.stream.write(self.text(text))
        self.stream.flush()

    def note(self, text: str) -> None:
        self.print(self.dim(text))

    def _flush_out(self) -> None:
        """Drain stdout before writing to stderr.

        stdout is block-buffered when it is a pipe while stderr is not, so
        without this a warning printed halfway through a command jumps ahead of
        every line that logically preceded it — which makes a captured log read
        as if the error happened first.
        """
        try:
            self.stream.flush()
        except (ValueError, OSError):  # closed or already-broken pipe
            pass

    def warn(self, text: str) -> None:
        self._flush_out()
        print(f"{self.paint('warning:', _YELLOW, _BOLD)} {self.text(text)}", file=self.err)

    def error(self, text: str) -> None:
        self._flush_out()
        print(f"{self.paint('error:', _RED, _BOLD)} {self.text(text)}", file=self.err)

    def hint(self, text: str) -> None:
        self._flush_out()
        arrow = "→" if self.unicode else "->"
        print(f"  {self.dim(arrow)} {self.text(text)}", file=self.err)

    # -- glyphs -----------------------------------------------------------

    @property
    def bar_chars(self) -> tuple[str, str]:
        return ("█", "░") if self.unicode else ("#", "-")

    _UNICODE_GLYPHS = {"ok": "✓", "warn": "!", "fail": "✗"}
    _ASCII_GLYPHS = {"ok": "OK", "warn": "WARN", "fail": "FAIL"}

    def status_symbol(self, status: str) -> str:
        """Return the ✓/!/✗ glyph (or its ASCII stand-in) for a check status."""
        glyphs = self._UNICODE_GLYPHS if self.unicode else self._ASCII_GLYPHS
        paint = {"ok": self.green, "warn": self.yellow, "fail": self.red}[status]
        return paint(glyphs[status])

    def status_cell(self, status: str) -> str:
        """The status glyph, painted and padded to :attr:`status_width`.

        Padding is applied to the *unpainted* glyph: ANSI escapes have zero
        display width but plenty of string length, so ``str.ljust`` on the
        coloured text would shove every column out of alignment.
        """
        glyphs = self._UNICODE_GLYPHS if self.unicode else self._ASCII_GLYPHS
        pad = " " * (self.status_width - len(glyphs[status]))
        return self.status_symbol(status) + pad

    @property
    def status_width(self) -> int:
        glyphs = self._UNICODE_GLYPHS if self.unicode else self._ASCII_GLYPHS
        return max(len(g) for g in glyphs.values())


def _is_tty(stream: TextIO) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):  # closed or exotic stream
        return False


def _encodable(stream: TextIO, probe: str) -> bool:
    """True when *probe* survives a round trip through *stream*'s encoding."""
    encoding = getattr(stream, "encoding", None) or "ascii"
    try:
        probe.encode(encoding)
    except (LookupError, UnicodeEncodeError):
        return False
    return True


#: Configured once in :func:`main`; commands write through this.
console = Console()


class CommandError(Exception):
    """A user-facing failure: printed as ``error: …`` plus an optional hint."""

    def __init__(self, message: str, hint: str = "", code: int = EXIT_FAILURE) -> None:
        super().__init__(message)
        self.hint = hint
        self.code = code
