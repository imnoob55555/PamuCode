"""Shared palette, glyphs and ANSI rendering for the interactive terminal.

Styling only applies to interactive terminals: redirected or captured output
keeps the plain text it always had, so transcripts and logs stay readable.
``NO_COLOR`` keeps the layout glyphs but drops every color.
"""

from __future__ import annotations

import io
import os
import shutil
import sys
from functools import lru_cache

from rich.color import ColorSystem
from rich.console import Console
from rich.style import Style
from rich.text import Text

ACCENT = "#d97757"
ACCENT_SOFT = "#eba27a"
TEXT = "#e6e6e6"
MUTED = "#8f8f8f"
DIM = "#5f5f5f"
BORDER = "#6c6c6c"
SUCCESS = "#4eba65"
ERROR = "#ff6b80"
WARNING = "#ffc107"
INFO = "#7aa2f7"
CODE = "#b9a5f5"
PLAN = "#48968c"

STYLES = {
    "accent": ACCENT,
    "accent.bold": f"bold {ACCENT}",
    "accent.soft": ACCENT_SOFT,
    "muted": MUTED,
    "dim": DIM,
    "bold": "bold",
    "success": SUCCESS,
    "error": ERROR,
    "error.bold": f"bold {ERROR}",
    "warning": WARNING,
    "warning.bold": f"bold {WARNING}",
    "info": INFO,
    "code": CODE,
    "plan": f"bold {PLAN}",
    "user": TEXT,
    "diff.add": f"{SUCCESS} on #16301f",
    "diff.remove": f"{ERROR} on #3a1820",
    "diff.hunk": INFO,
}

BULLET = "●"
USER_MARK = ">"
PROMPT_MARK = "❯"
SPINNER_FRAMES = ("·", "✢", "✳", "✶", "✻", "✽", "✻", "✶", "✳", "✢")
_COLOR_SYSTEMS = {
    "standard": ColorSystem.STANDARD,
    "256": ColorSystem.EIGHT_BIT,
    "truecolor": ColorSystem.TRUECOLOR,
    "windows": ColorSystem.WINDOWS,
}


def rich_output(stream=None) -> bool:
    """Whether ``stream`` is an interactive terminal that can show the UI."""
    stream = sys.stdout if stream is None else stream
    if os.environ.get("TERM") == "dumb":
        return False
    try:
        return bool(stream.isatty())
    except (AttributeError, OSError, ValueError):
        return False


def color_enabled(stream=None) -> bool:
    return rich_output(stream) and "NO_COLOR" not in os.environ


@lru_cache(maxsize=8)
def _detect_color_system(_term, _colorterm) -> str | None:
    return Console(file=io.StringIO(), force_terminal=True).color_system


def terminal_color_system() -> str | None:
    return _detect_color_system(os.environ.get("TERM"), os.environ.get("COLORTERM"))


def color_system(stream=None) -> str | None:
    return terminal_color_system() if color_enabled(stream) else None


def paint(text: str, style: str, stream=None) -> str:
    """Wrap ``text`` in ANSI codes for ``style`` (a key of STYLES or a rich style)."""
    system = color_system(stream)
    if system is None or not text:
        return text
    return Style.parse(STYLES.get(style, style)).render(text, color_system=_COLOR_SYSTEMS[system])


def render(renderable, *, width: int, stream=None, color: bool | None = None) -> str:
    """Render a rich renderable to a string for ``stream`` (plain when not a TTY).

    ``color`` overrides the stream check when the caller already decided.
    """
    if color is None:
        system = color_system(stream)
    else:
        system = terminal_color_system() if color else None
    console = Console(
        file=io.StringIO(),
        width=max(1, width),
        force_terminal=system is not None,
        color_system=system,
        no_color=system is None,
        highlight=False,
        emoji=False,
        legacy_windows=False,
    )
    with console.capture() as capture:
        console.print(renderable, end="")
    return capture.get().removesuffix("\n")


def styled(text: str, style: str) -> Text:
    return Text(text, style=STYLES.get(style, style))


def success(message: str, stream=None) -> str:
    """Prefix a completed action with a green check on interactive terminals."""
    if not rich_output(stream):
        return message
    return f"{paint('✓', 'success', stream)} {message}"


def bullet(style: str, stream=None) -> str:
    return paint(BULLET, style, stream)


def user_message(text: str, stream=None) -> str:
    """Echo a submitted prompt as ``> text`` with a hanging indent."""
    if not rich_output(stream):
        return text
    width = max(10, shutil.get_terminal_size(fallback=(80, 24)).columns - 2)
    rendered = render(Text(text, style=STYLES["user"]), width=width, stream=stream)
    mark = paint(USER_MARK, "muted", stream)
    return "\n".join(
        (f"{mark} " if index == 0 else "  ") + line
        for index, line in enumerate(rendered.split("\n"))
    )
