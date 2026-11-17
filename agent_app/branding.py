"""Pure rendering helpers for the interactive PamuCode welcome screen."""

LOGO = r'''  ____    _    __  __ _   _
 |  _ \  / \  |  \/  | | | |
 | |_) |/ _ \ | |\/| | | | |
 |  __// ___ \| |  | | |_| |
 |_|  /_/   \_\_|  |_|\___/'''
TITLE = "PamuCode · AI Coding Agent"
START_HINT = "输入问题开始对话，输入 /help 查看命令"
EXIT_HINT = "输入 /quit 或在 1 秒内连按两次 Ctrl+C 退出"


def render_startup_screen(*, color=False, width=80):
    heading = LOGO + "\n\n" + TITLE if width >= 48 else TITLE
    if color:
        heading = "\033[36m" + heading + "\033[0m"
    return "\n".join(
        (
            heading,
            "",
            START_HINT,
            EXIT_HINT,
            "",
        )
    )
