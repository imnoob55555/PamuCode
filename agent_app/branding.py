"""Pure rendering helpers for the interactive PamuCode welcome screen."""

LOGO = r'''  ____    _    __  __ _   _
 |  _ \  / \  |  \/  | | | |
 | |_) |/ _ \ | |\/| | | | |
 |  __// ___ \| |  | | |_| |
 |_|  /_/   \_\_|  |_|\___/'''
TITLE = "PamuCode · AI Coding Agent"
START_HINT = "输入问题开始对话，输入 /help 查看命令"
EXIT_HINT = "输入 /quit 或在 1 秒内连按两次 Ctrl+C 退出"


def _display(value):
    return "".join(char if char.isprintable() else "?" for char in str(value))


def render_startup_screen(
    *, cwd, provider, model, configuration_error=False, color=False, width=80
):
    heading = LOGO + "\n\n" + TITLE if width >= 48 else TITLE
    if color:
        heading = "\033[36m" + heading + "\033[0m"
    ready = bool(provider and model) and not configuration_error
    state = (
        "配置异常，请输入 /login 修复"
        if configuration_error
        else "已准备就绪"
        if ready
        else "尚未配置 API Key，请输入 /login"
    )
    return "\n".join(
        (
            heading,
            "",
            f"工作目录：{_display(cwd)}",
            f"Provider：{_display(provider or '未配置')}",
            f"模型：{_display(model or '未配置')}",
            f"状态：{state}",
            "",
            START_HINT,
            EXIT_HINT,
            "",
        )
    )
