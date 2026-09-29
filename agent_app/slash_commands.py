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
    SlashCommand("/login", "", "配置 API Key 并选择模型"),
    SlashCommand("/status", "", "查看供应商、模型及登录状态"),
    SlashCommand("/model", "", "选择或切换当前模型"),
    SlashCommand("/provider", "", "选择并切换供应商"),
    SlashCommand("/thinking", "", "选择或切换模型思考程度"),
    SlashCommand("/help", "", "显示命令帮助", ("/h",)),
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
        + "\n".join(f"  {item.usage:<23} {item.description}" for item in SLASH_COMMANDS)
    )
