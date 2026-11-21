"""Rich terminal blocks: streamed Markdown lines, approvals, diffs and help.

Every public helper here assumes an interactive terminal; callers keep their
plain-text output when :func:`theme.rich_output` is false.
"""

from __future__ import annotations

import re
import shutil

from prompt_toolkit.utils import get_cwidth
from rich import box
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from . import theme

FENCE = re.compile(r"^\s*(`{3,}|~{3,})\s*([\w+#.-]*)\s*$")
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
RULE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
QUOTE = re.compile(r"^\s*>\s?(.*)$")
TASK = re.compile(r"^(\s*)[-*+]\s+\[([ xX])\]\s+(.*)$")
LIST_ITEM = re.compile(r"^(\s*)[-*+]\s+(.*)$")
ORDERED = re.compile(r"^(\s*)(\d+[.)])\s+(.*)$")
TABLE_RULE = re.compile(r"^\s*\|?(\s*:?-{3,}:?\s*\|)+\s*(:?-{3,}:?)?\s*\|?\s*$")
INLINE = re.compile(
    r"(`+)(.+?)\1"
    r"|\*\*(.+?)\*\*"
    r"|__(.+?)__"
    r"|~~(.+?)~~"
    r"|(?<![\w*])\*(?![\s*])(.+?)(?<![\s*])\*(?![\w*])"
    r"|\[([^\]]+)\]\(([^)\s]+)\)"
)
BULLETS = ("•", "◦", "▪")
INDENT = "  "


def terminal_width() -> int:
    return max(20, shutil.get_terminal_size(fallback=(80, 24)).columns)


def inline(source: str, style: str = "") -> Text:
    """Style inline Markdown: code spans, bold, italics, strikethrough, links."""
    result = Text(style=style)
    position = 0
    for match in INLINE.finditer(source):
        result.append(source[position:match.start()])
        code, bold, bold_alt, strike, italic, label, url = match.group(2, 3, 4, 5, 6, 7, 8)
        if code is not None:
            result.append(code, style=theme.STYLES["code"])
        elif bold or bold_alt:
            result.append(inline(bold or bold_alt, "bold"))
        elif strike:
            result.append(strike, style="strike")
        elif italic:
            result.append(inline(italic, "italic"))
        else:
            result.append(label, style=f"underline {theme.INFO}")
            if url != label:
                result.append(f" ({url})", style=theme.MUTED)
        position = match.end()
    result.append(source[position:])
    return result


class MarkdownLines:
    """Style Markdown one complete line at a time, tracking code fences."""

    def __init__(self):
        self.fence = ""
        self.language = ""

    def style(self, line: str) -> Text:
        fence = FENCE.match(line)
        if fence and (not self.fence or (fence[1][0] == self.fence[0] and len(fence[1]) >= len(self.fence)
                                         and not fence[2])):
            self.fence, self.language = ("", "") if self.fence else (fence[1], fence[2])
            return Text(line.strip(), style=theme.DIM)
        if self.fence:
            return self._code(line)
        if match := HEADING.match(line):
            level = len(match[1])
            style = (f"bold underline {theme.ACCENT}" if level == 1
                     else f"bold {theme.ACCENT}" if level == 2 else "bold")
            return inline(match[2], style)
        if RULE.match(line):
            return Text("─" * min(40, terminal_width() - 4), style=theme.DIM)
        if match := TASK.match(line):
            done = match[2].lower() == "x"
            return Text.assemble(
                match[1],
                ("☑ " if done else "☐ ", theme.SUCCESS if done else theme.MUTED),
                inline(match[3], theme.MUTED if done else ""),
            )
        if match := LIST_ITEM.match(line):
            depth = len(match[1].expandtabs(4)) // 2
            return Text.assemble(match[1], (BULLETS[depth % len(BULLETS)] + " ", theme.ACCENT), inline(match[2]))
        if match := ORDERED.match(line):
            return Text.assemble(match[1], (match[2] + " ", theme.ACCENT), inline(match[3]))
        if match := QUOTE.match(line):
            return Text.assemble(("▎ ", theme.DIM), inline(match[1], f"italic {theme.MUTED}"))
        if line.lstrip().startswith("|"):
            if TABLE_RULE.match(line):
                return Text(line.replace("-", "─").replace("|", "│"), style=theme.DIM)
            cells = line.split("|")
            row = Text()
            for index, cell in enumerate(cells):
                if index:
                    row.append("│", style=theme.DIM)
                row.append(inline(cell))
            return row
        return inline(line)

    def _code(self, line: str) -> Text:
        if not self.language:
            return Text(line, style=theme.STYLES["code"])
        try:
            text = Syntax(line, self.language, theme="ansi_dark", background_color="default").highlight(line)
        except Exception:
            return Text(line, style=theme.STYLES["code"])
        text.rstrip()
        return text


def wrapped_rows(start_column: int, text: str, columns: int) -> int:
    """Terminal rows occupied by ``text`` written from ``start_column``."""
    row, column = 0, start_column
    for char in text:
        width = max(0, get_cwidth(char))
        if column + width > columns:
            row, column = row + 1, 0
        column += width
    return row + 1


def indent_block(rendered: str, first: str, rest: str = INDENT) -> str:
    lines = rendered.split("\n")
    return "\n".join((first if index == 0 else rest) + line for index, line in enumerate(lines))


def render_line(text: Text, *, lead: str, stream=None) -> str:
    """Render one styled line wrapped under ``lead`` with a hanging indent."""
    width = terminal_width() - get_cwidth(INDENT)
    return indent_block(theme.render(text, width=width, stream=stream), lead)


def render_markdown(source: str, *, stream=None) -> str:
    """Render a complete assistant message as a bulleted block."""
    lines = MarkdownLines()
    output = []
    for line in source.strip("\n").split("\n"):
        lead = f"{theme.bullet('bold', stream)} " if not output else INDENT
        output.append(render_line(lines.style(line.expandtabs(4)), lead=lead, stream=stream) if line else "")
    return "\n".join(output)


def approval_panel(title: str, rows: list[tuple[str, str]], *, stream=None) -> str:
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style=theme.MUTED, no_wrap=True)
    grid.add_column()
    for label, value in rows:
        grid.add_row(Text(label), Text(value))
    panel = Panel(
        grid,
        title=Text(f"⚠ {title}", style=theme.STYLES["warning.bold"]),
        title_align="left",
        box=box.ROUNDED,
        border_style=theme.WARNING,
        padding=(0, 1),
        width=min(terminal_width(), 100),
    )
    return "\n" + theme.render(panel, width=terminal_width(), stream=stream)


def blocked_notice(reason: str, *, stream=None) -> str:
    return f"\n{theme.bullet('error', stream)} {theme.paint('Blocked', 'error.bold', stream)} {reason}"


def diff_block(diff_lines: list[str], *, stream=None) -> str:
    """Indented, colored unified diff shown under the tool call header."""
    output = []
    body = [line for line in diff_lines if not line.startswith(("---", "+++"))]
    if not body:
        output.append(INDENT + theme.paint("(no diff)", "muted", stream))
    for line in body:
        style = ("diff.hunk" if line.startswith("@@") else "diff.add" if line.startswith("+")
                 else "diff.remove" if line.startswith("-") else "muted")
        output.append(INDENT + theme.paint(line, style, stream))
    return "\n".join(output)


def help_block(commands, *, stream=None) -> str:
    wide = terminal_width() >= 90
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style=theme.STYLES["accent.bold"], no_wrap=True)
    grid.add_column(overflow="fold")
    if wide:
        grid.add_column(style=theme.DIM, overflow="fold")
    for item in commands:
        cells = [Text(item.name), Text(item.description)]
        grid.add_row(*cells, *([Text(item.arguments)] if wide else []))
    heading = theme.paint("可用命令", "bold", stream)
    return heading + "\n" + indent_block(theme.render(grid, width=terminal_width() - 2, stream=stream), INDENT)


def section(title: str, *, stream=None) -> str:
    width = min(terminal_width(), 80)
    tail = "─" * max(3, width - get_cwidth(title) - 4)
    return theme.paint(f"── {title} {tail}", "dim", stream)


def transcript_entry(role: str, text: str, *, stream=None) -> str:
    if role == "用户":
        return theme.user_message(text, stream)
    if role == "PamuCode":
        return render_markdown(text, stream=stream)
    command = indent_block(theme.paint(text, "muted", stream), INDENT)
    return f"{theme.bullet('dim', stream)} {theme.paint(role, 'muted', stream)}\n{command}"
