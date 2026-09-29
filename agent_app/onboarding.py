"""Offline provider setup; persistence order is independent of terminal UI."""

from pathlib import Path

from .adapters import providers
from .features import auth


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
        key = environment.get(spec.key_env) or record.get("api_key")
        configured = isinstance(key, str) and bool(key.strip())
        state = "已配置" if configured else "未配置 API Key"
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

        def save_key(value, _spec=spec):
            auth.save_key(_spec.name, value, path)

        key = ui.secret(
            f"{spec.label} · 输入 API Key",
            on_save=save_key,
        )
        if key is None:
            _notify_or_print(ui, "已取消登录。")
            if provider:
                return None
            continue
        print("API Key 已保存到本地。")
        record = auth.get_login(auth.load_credentials(path), spec.name)
        while True:
            model = choose_model(ui, spec, record.get("model"))
            if model is None:
                _notify_or_print(
                    ui,
                    "已取消模型选择，API Key 已保留；可用 /login 或 /model 继续配置。"
                )
                return None
            try:
                auth.store_login(spec.name, key, model, path)
            except OSError:
                _notify_or_print(
                    ui,
                    "模型保存失败，请检查凭证文件的写入权限或磁盘空间后重试。",
                )
                continue
            print(f"已选择 {spec.label} · {model}。")
            return spec.name
