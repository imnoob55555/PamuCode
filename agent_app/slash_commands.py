"""Shared command metadata for help and live suggestions."""

from dataclasses import dataclass


@dataclass(frozen=True)
class SlashCommand:
    name: str
    arguments: str
    description: str
    aliases: tuple[str, ...] = ()

    @property
    def usage(self) -> str:
        return f"{self.name} {self.arguments}".rstrip()


SLASH_COMMANDS = (
    SlashCommand("/login", "", "配置模型服务与认证"),
    SlashCommand("/status", "", "查看供应商、模型及登录状态"),
    SlashCommand("/model", "", "选择或切换当前模型"),
    SlashCommand("/provider", "", "选择并切换供应商"),
    SlashCommand("/thinking", "", "选择或切换模型思考程度"),
    SlashCommand("/context", "[window <tokens> | pin <文本> | pins | unpin <编号>]", "查看上下文预算及管理固定约束"),
    SlashCommand("/compact", "[focus]", "压缩旧历史并保留近期交互"),
    SlashCommand("/resume", "[--last | 会话编号]", "恢复当前工作区的其他会话"),
    SlashCommand("/delete", "", "删除当前会话并退出"),
    SlashCommand("/background", "", "查看当前会话的后台命令"),
    SlashCommand("/stop", "<后台任务编号 | all>", "停止指定后台命令或全部后台命令"),
    SlashCommand("/help", "", "显示命令帮助", ("/h",)),
    SlashCommand("/plan", "", "进入规划模式，只读探索并制定计划"),
    SlashCommand("/execute", "", "进入执行模式，按用户要求实施"),
    SlashCommand("/skill", "[名称] [任务描述]", "选择并调用技能"),
    SlashCommand("/quit", "", "退出当前 PamuCode 会话"),
)


def match_commands(text: str) -> list[SlashCommand]:
    text = text.lstrip()
    if not text.startswith("/"):
        return []
    keyword = text.split(maxsplit=1)[0][1:].casefold()
    matches = [
        item
        for item in SLASH_COMMANDS
        if keyword in item.name[1:].casefold()
        or keyword in item.description.casefold()
        or f"/{keyword}" in item.aliases
    ]
    return sorted(
        matches, key=lambda item: not item.name[1:].casefold().startswith(keyword)
    )


def help_text() -> str:
    return (
        "可用命令：\n"
        + "\n".join(
            f"  {item.name}  {item.description}"
            for item in SLASH_COMMANDS
        )
    )
