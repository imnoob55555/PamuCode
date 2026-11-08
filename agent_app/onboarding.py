"""Offline provider setup; persistence order is independent of terminal UI."""

from pathlib import Path

from .adapters import providers
from .features import auth
from .provider_config import normalize_base_url, resolve_provider_config


def _notify_or_print(ui, message):
    notify = getattr(ui, "notify", None)
    if callable(notify):
        notify(message)
    else:
        print(message)


def choose_provider(ui, credentials, environment, current=None) -> str | None:
    choices = []
    specs = sorted(providers.PROVIDERS.values(), key=lambda spec: spec.name != current)
    for spec in specs:
        record = auth.get_login(credentials, spec.name)
        try:
            resolve_provider_config(environment, credentials, spec.name)
            configured = True
        except ValueError:
            configured = False
        state = "已配置" if configured else ("未配置 API Key" if spec.requires_key else "未配置服务/模型")
        if spec.name == current:
            state = "当前"
        choices.append((spec.name, f"{spec.label} ({spec.name}) · {state}"))
    return ui.choose("切换 Provider", choices)


def choose_model(ui, spec, current=None) -> str | None:
    choices = [(model, model) for model in providers.model_choices(spec, current)]
    choices.append(("", "自定义模型 ID…"))
    model = ui.choose(f"选择模型 · {spec.label}（内置列表）", choices)
    if model == "":
        return ui.text("输入自定义模型 ID")
    return model


def login(ui, path: Path, provider: str | None = None) -> str | None:
    while True:
        name = provider or ui.choose(
            "选择 Provider",
            [
                (spec.name, f"{spec.label} ({spec.name})")
                for spec in providers.PROVIDERS.values()
            ],
        )
        if name is None:
            _notify_or_print(ui, "已取消登录。")
            return None
        spec = providers.get_provider(name)
        record = auth.get_login(auth.load_credentials(path), spec.name)
        base_url = protocol = None
        if spec.name in ("ollama", "openai-compatible"):
            if spec.name == "openai-compatible":
                protocol = ui.choose("选择接口协议", [
                    ("openai_chat", "OpenAI Chat Completions"),
                    ("openai_responses", "OpenAI Responses"),
                ])
                if protocol is None:
                    return None
            else:
                protocol = spec.protocol
            default_url = record.get("base_url") or spec.base_url
            if default_url:
                base_url = ui.choose("服务地址", [(default_url, default_url), ("", "自定义地址…")])
                if base_url is None:
                    return None
            while not base_url:
                value = ui.text("输入服务 Base URL（包含服务要求的路径，如 /v1）")
                if value is None:
                    return None
                try:
                    base_url = normalize_base_url(value)
                except ValueError as error:
                    _notify_or_print(ui, str(error))

        def save_key(value, _spec=spec):
            auth.save_key(_spec.name, value, path)

        needs_key = spec.requires_key
        if spec.name == "openai-compatible":
            authentication = ui.choose("服务认证", [("none", "无需 API Key（本地服务）"), ("key", "使用 API Key")])
            if authentication is None:
                return None
            needs_key = authentication == "key"
        key = ui.secret(f"{spec.label} · 输入 API Key", on_save=save_key) if needs_key else ""
        if key is None:
            _notify_or_print(ui, "已取消登录。")
            if provider:
                return None
            continue
        if needs_key:
            print("API Key 已保存到本地。")
        record = auth.get_login(auth.load_credentials(path), spec.name)
        while True:
            model = choose_model(ui, spec, record.get("model"))
            if model is None:
                if not needs_key:
                    notice = "已取消模型选择。"
                elif spec.name == "openai-compatible":
                    notice = "已取消模型选择，API Key 已保留；请用 /login 重新配置服务。"
                else:
                    notice = "已取消模型选择，API Key 已保留；可用 /login 或 /model 继续配置。"
                _notify_or_print(
                    ui, notice,
                )
                return None
            try:
                auth.store_login(spec.name, key, model, path, base_url=base_url, protocol=protocol)
            except OSError:
                _notify_or_print(
                    ui,
                    "模型保存失败，请检查凭证文件的写入权限或磁盘空间后重试。",
                )
                continue
            print(f"已选择 {spec.label} · {model}。")
            return spec.name
