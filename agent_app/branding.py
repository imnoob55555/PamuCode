"""Pure rendering helpers for the interactive PamuCode welcome screen."""

from importlib import metadata

from rich import box
from rich.color import Color
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import theme

LOGO = """\
██████╗  █████╗ ███╗   ███╗██╗   ██╗
██╔══██╗██╔══██╗████╗ ████║██║   ██║
██████╔╝███████║██╔████╔██║██║   ██║
██╔═══╝ ██╔══██║██║╚██╔╝██║██║   ██║
██║     ██║  ██║██║ ╚═╝ ██║╚██████╔╝
╚═╝     ╚═╝  ╚═╝╚═╝     ╚═╝ ╚═════╝"""
TITLE = "PamuCode · AI Coding Agent"
START_HINT = "输入问题开始对话，输入 /help 查看命令"
EXIT_HINT = "输入 /quit 或在 1 秒内连按两次 Ctrl+C 退出"
SHORTCUTS = (("/", "命令"), ("@", "引用文件"), ("↑↓", "历史"), ("Ctrl+C", "中断"))
GRADIENT = ("#f6c177", "#eba27a", "#d97757", "#c4677e")
PANEL_WIDTH = 54


def _version() -> str:
    try:
        return metadata.version("pamucode")
    except metadata.PackageNotFoundError:
        return ""


def _blend(stops, position: float) -> Color:
    """Interpolate a color along ``stops`` at ``position`` in [0, 1]."""
    scaled = position * (len(stops) - 1)
    index = min(int(scaled), len(stops) - 2)
    start = Color.parse(stops[index]).get_truecolor()
    end = Color.parse(stops[index + 1]).get_truecolor()
    fraction = scaled - index
    return Color.from_rgb(*(a + (b - a) * fraction for a, b in zip(start, end)))


def gradient_logo() -> Text:
    """The block logo with a warm left-to-right gradient and dimmed shadows."""
    rows = LOGO.split("\n")
    width = max(len(row) for row in rows)
    logo = Text(no_wrap=True)
    for number, row in enumerate(rows):
        for column, char in enumerate(row):
            color = _blend(GRADIENT, column / max(1, width - 1))
            style = f"bold {color.name}" if char == "█" else f"{color.name} dim"
            logo.append(char, style=style)
        if number < len(rows) - 1:
            logo.append("\n")
    return logo


def _shortcuts() -> Text:
    text = Text()
    for index, (key, label) in enumerate(SHORTCUTS):
        if index:
            text.append("   ")
        text.append(key, style=theme.STYLES["accent.soft"])
        text.append(f" {label}", style=theme.MUTED)
    return text


def render_startup_screen(*, color=False, width=80):
    heading = Table.grid(expand=True, padding=(0, 2))
    heading.add_column()
    heading.add_column(justify="right")
    version = _version()
    heading.add_row(
        Text.assemble(("✻ ", theme.STYLES["accent.bold"]), (TITLE, "bold")),
        Text(f"v{version}" if version else "", style=theme.MUTED),
    )
    parts = [gradient_logo(), Text()] if width >= 48 else []
    parts += [
        heading,
        Text(),
        Text(START_HINT, style=theme.TEXT),
        Text(EXIT_HINT, style=theme.MUTED),
        Text(),
        _shortcuts(),
    ]
    panel = Panel(
        Group(*parts),
        box=box.ROUNDED,
        border_style=theme.ACCENT,
        padding=(1, 2),
        width=min(width, PANEL_WIDTH),
    )
    return theme.render(panel, width=max(20, width), color=bool(color)) + "\n"
