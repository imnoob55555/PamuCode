"""Interactive command-line lifecycle for the agent runtime."""

from __future__ import annotations

import getpass
from contextlib import nullcontext
import os
import shutil
import sys
import threading
from pathlib import Path

from .adapters import providers
from .provider_config import resolve_provider_config, normalize_base_url, ProviderConfigurationError
from .bootstrap import build_default_runtime
from .branding import render_startup_screen
from .cli_exit import ExitAction, ExitGesture, QuitRequested
from .config import MissingConfigurationError
from .core.loop import run_agent_loop
from .features.sessions import checkpoint
from .features.session_store import SessionError
from .features import auth
from .features.scheduler import (
    cron_scheduler_loop,
    has_cron_queue,
    load_durable_jobs,
)
from .logging_setup import get_logger
from .slash_commands import SLASH_COMMANDS, help_text
from .thinking import THINKING_LABELS, THINKING_LEVELS, normalize_level

LOGGER = get_logger("pamu.cli")
COMMANDS = ("login", "logout", "status", "use")

SLASH_HELP = help_text()
LOGIN_HINT = "尚未配置模型服务，请输入 /login 选择供应商、服务地址和模型。"


def notify_or_print(ui, message: str, *, print_fn=print) -> None:
    notify = getattr(ui, "notify", None)
    if callable(notify):
        notify(message)
    else:
        print_fn(message)


def run_agent_turn(runtime, user_query: str | None = None) -> None:
    # Slash commands are REPL-local controls; they never join the LLM context.
    if user_query and not user_query.lstrip().startswith("/"):
        runtime.session.history.append({"role": "user", "content": user_query})
    checkpoint(runtime, status="running")
    try:
        run_agent_loop(runtime)
    except BaseException:
        coordinator = getattr(runtime, "sessions", None)
        if coordinator is None or not coordinator.failed:
            checkpoint(runtime, status="interrupted")
        raise
    else:
        checkpoint(runtime, status="idle")
    print()


def _queue_processor_loop(runtime) -> None:
    while not runtime.stop_event.wait(0.2):
        if not has_cron_queue(runtime.scheduler):
            continue
        if not runtime.agent_lock.acquire(blocking=False):
            continue
        try:
            if getattr(runtime.stop_event, "is_set", lambda: False)():
                return
            if has_cron_queue(runtime.scheduler):
                print("\n  \033[35m[queue processor] delivering scheduled work\033[0m")
                run_agent_turn(runtime)
        except SessionError as exc:
            print(str(exc))
            runtime.stop_event.set()
            return
        finally:
            runtime.agent_lock.release()


def start_runtime_threads(runtime) -> list[threading.Thread]:
    load_durable_jobs(runtime.scheduler, runtime.config)
    scheduler = threading.Thread(
        target=cron_scheduler_loop,
        args=(runtime.scheduler, runtime.config, runtime.stop_event),
        daemon=True,
        name="cron-scheduler",
    )
    processor = threading.Thread(
        target=_queue_processor_loop,
        args=(runtime,),
        daemon=True,
        name="cron-queue-processor",
    )
    threads = [scheduler, processor]
    started = []
    try:
        for thread in threads:
            thread.start()
            started.append(thread)
    except Exception:
        runtime.stop_event.set()
        for thread in started:
            thread.join(timeout=1.0)
        raise
    return threads


def stop_runtime_threads(runtime, threads) -> None:
    runtime.stop_event.set()
    for thread in threads:
        thread.join(timeout=1.0)


# ---------------------------------------------------------------------------
# Provider account commands (pamu login / logout / status / use)
# ---------------------------------------------------------------------------


def _ask(prompt: str, input_fn) -> str:
    """Read one line, degrading EOF into an empty cancel instead of a crash."""
    try:
        return input_fn(prompt)
    except EOFError:
        print()
        return ""


def _pick_provider_interactively(input_fn) -> str | None:
    names = sorted(providers.PROVIDERS)
    print("可用供应商：")
    for index, name in enumerate(names, start=1):
        spec = providers.PROVIDERS[name]
        print(f"  {index}. {name:<11} {spec.label}")
    choice = _ask("选择供应商（名称或序号）: ", input_fn).strip()
    if not choice:
        return None
    if choice.isdigit():
        try:
            position = int(choice) - 1
        except ValueError:
            return None
        if 0 <= position < len(names):
            return names[position]
        return None
    return choice


def _cmd_login(
    args: list[str],
    *,
    credentials_path: Path,
    verify,
    client_factory,
    input_fn,
    secret_fn,
) -> int:
    skip_verify = "--no-verify" in args
    args = [item for item in args if item != "--no-verify"]
    name = args[0] if args else _pick_provider_interactively(input_fn)
    if not name:
        print("未选择供应商。", file=sys.stderr)
        return 2
    try:
        spec = providers.get_provider(name)
    except providers.UnknownProviderError:
        print(
            f"未知供应商：{name}。可用：{', '.join(sorted(providers.PROVIDERS))}",
            file=sys.stderr,
        )
        return 2

    import os
    from dataclasses import replace
    from .bootstrap import load_runtime_environment

    environment = {**load_runtime_environment(), **os.environ}
    record = auth.get_login(auth.load_credentials(credentials_path), spec.name)
    url_name = spec.base_url_env or spec.name.upper() + "_BASE_URL"
    configured_url = environment.get(url_name) or record.get("base_url")
    if configured_url:
        try:
            spec = replace(spec, base_url=normalize_base_url(configured_url))
        except ValueError as error:
            print(str(error), file=sys.stderr)
            return 2
    api_key = environment.get(spec.key_env) if spec.key_env else None
    if api_key:
        print(f"使用环境变量 {spec.key_env} 中的密钥。")
    elif spec.requires_key:
        api_key = secret_fn(f"{spec.label} API Key: ").strip()
        if not api_key:
            print("未提供 API Key，已取消登录。", file=sys.stderr)
            return 2

    base_url, protocol = configured_url, None
    if spec.name in ("ollama", "openai-compatible"):
        protocol = spec.protocol
        if spec.name == "openai-compatible":
            protocol = environment.get("OPENAI_COMPATIBLE_PROTOCOL") or record.get("protocol") or protocol
            protocol = _ask(f"接口协议 openai_chat / openai_responses [{protocol}]: ", input_fn).strip() or protocol
            if protocol not in ("openai_chat", "openai_responses"):
                print("无效接口协议。", file=sys.stderr)
                return 2
        try:
            base_url = normalize_base_url(
                _ask(f"服务 Base URL [{spec.base_url}]: ", input_fn).strip() or spec.base_url
            )
        except ValueError as error:
            print(str(error), file=sys.stderr)
            return 2
        if spec.name == "openai-compatible" and not api_key:
            api_key = secret_fn("API Key（本地服务可留空）: ").strip()
        spec = replace(spec, base_url=base_url, protocol=protocol)
    default_model = environment.get("MODEL_ID") or record.get("model") or spec.default_model
    model = _ask(f"默认模型 [{default_model}]: ", input_fn).strip() or default_model
    if not model:
        print("必须填写模型 ID。", file=sys.stderr)
        return 2
    if not skip_verify:
        ok = verify(spec, api_key, anthropic_factory=client_factory, model=model)
        if not ok:
            print(
                f"登录验证失败：{spec.label} 拒绝了该密钥或网络不可用。\n"
                "可使用 `pamu login <name> --no-verify` 跳过验证。",
                file=sys.stderr,
            )
            return 1

    auth.store_login(spec.name, api_key or "", model, credentials_path, base_url=base_url, protocol=protocol)
    LOGGER.info("login provider=%s model=%s", spec.name, model)
    print(f"已登录 {spec.label} 并设为当前供应商（模型 {model}）。")
    print(f"下次启动生效；会话内可用 /provider {spec.name} 立即切换。")
    return 0


def _cmd_logout(
    args: list[str],
    *,
    credentials_path: Path,
    **_unused,
) -> int:
    data = auth.load_credentials(credentials_path)
    if args:
        for name in args:
            if name not in providers.PROVIDERS and name not in data["providers"]:
                print(f"未知供应商：{name}", file=sys.stderr)
                return 2
            auth.remove_login(name, credentials_path)
            print(f"已登出 {name}。")
        return 0
    for name in list(data["providers"]):
        auth.remove_login(name, credentials_path)
    print("已登出全部供应商，回到环境变量/Anthropic 默认模式。")
    return 0


def _cmd_status(
    _args: list[str],
    *,
    credentials_path: Path,
    **_unused,
) -> int:
    data = auth.load_credentials(credentials_path)
    active = data.get("active") or "anthropic（环境变量模式）"
    from .bootstrap import load_runtime_environment

    try:
        resolved = resolve_provider_config(load_runtime_environment(), data)
    except ValueError:
        resolved = None
    if resolved is not None:
        active = resolved.spec.name
    print(f"当前供应商：{active}")
    if resolved is not None:
        print(f"协议：{resolved.protocol}")
        print(f"服务地址：{resolved.base_url}")
        print(f"有效模型：{resolved.model}（{resolved.model_source}）")
    records = data.get("providers", {})
    if records:
        print("已登录：")
        for name in sorted(records):
            record = auth.get_login(data, name)
            marker = " ← 当前" if data.get("active") == name else ""
            print(
                f"  {name:<11} 模型 {record.get('model', '(默认)')}{marker}"
            )
    else:
        print("已登录：（无，使用 ANTHROPIC_API_KEY 等环境变量）")
    print(f"凭证文件：{credentials_path}")
    print(f"全局配置：{Path.home() / '.pamu' / '.settings'}")
    return 0


def _cmd_use(
    args: list[str],
    *,
    credentials_path: Path,
    **_unused,
) -> int:
    if not args:
        print("用法：pamu use <provider>", file=sys.stderr)
        return 2
    name = args[0]
    try:
        spec = providers.get_provider(name)
    except providers.UnknownProviderError:
        print(
            f"未知供应商：{name}。可用：{', '.join(sorted(providers.PROVIDERS))}",
            file=sys.stderr,
        )
        return 2
    data = auth.load_credentials(credentials_path)
    record = auth.get_login(data, spec.name)
    from .bootstrap import load_runtime_environment

    try:
        resolved = resolve_provider_config(load_runtime_environment(), data, spec.name)
    except ValueError:
        print(f"尚未登录 {spec.label}，请先运行：pamu login {spec.name}", file=sys.stderr)
        return 2
    auth.set_active(spec.name, credentials_path)
    model = resolved.model
    LOGGER.info("use provider=%s model=%s", spec.name, model)
    print(f"已切换到 {spec.label}（模型 {model}），下次启动 pamu 时生效。")
    return 0


def run_command(
    args: list[str],
    *,
    credentials_path: Path | None = None,
    verify=providers.verify_login,
    client_factory=None,
    input_fn=input,
    secret_fn=None,
) -> int:
    """Dispatch one provider account command; return the process exit code."""
    command, rest = args[0], args[1:]
    credentials_path = credentials_path or auth.CREDENTIALS_PATH
    context = {
        "credentials_path": credentials_path,
        "verify": verify,
        "client_factory": client_factory,
        "input_fn": input_fn,
        "secret_fn": secret_fn or getpass.getpass,
    }
    if command == "login":
        return _cmd_login(rest, **context)
    if command == "logout":
        return _cmd_logout(rest, **context)
    if command == "status":
        return _cmd_status(rest, **context)
    if command == "use":
        return _cmd_use(rest, **context)
    print(f"未知命令：{command}", file=sys.stderr)
    return 2


# ---------------------------------------------------------------------------
# REPL slash commands (/status /model /provider /help)
# ---------------------------------------------------------------------------


def handle_slash_command(runtime, query: str, *, credentials_path=None,
                         client_factory=None, print_fn=print, ui=None):
    """Keep model changes and their durable checkpoint in one agent transaction."""
    mutating = query.strip().partition(" ")[0] in ("/model", "/provider", "/thinking")
    lock = getattr(runtime, "agent_lock", None) if mutating else None
    with lock if lock is not None else nullcontext():
        fields = ("provider", "protocol", "base_url", "primary_model", "fallback_model", "thinking_level")
        router = getattr(runtime, "model", None)
        before = tuple(getattr(router, key, None) for key in fields)
        result = _handle_slash_command(
            runtime, query, credentials_path=credentials_path,
            client_factory=client_factory, print_fn=print_fn, ui=ui,
        )
        after = tuple(getattr(router, key, None) for key in fields)
        if mutating and before != after:
            checkpoint(runtime)
        return result


def _handle_slash_command(
    runtime,
    query: str,
    *,
    credentials_path: Path | None = None,
    client_factory=None,
    print_fn=print,
    ui=None,
) -> str | ExitAction | None:
    """Handle one slash command inside the REPL, mutating runtime on switch."""
    credentials_path = credentials_path or auth.CREDENTIALS_PATH
    command, _, rest = query.strip().partition(" ")
    argument = rest.strip()

    known_names = {
        name for item in SLASH_COMMANDS for name in (item.name, *item.aliases)
    }
    if command not in known_names:
        notify_or_print(
            ui,
            f"未知命令：{command}。输入 /help 查看命令。",
            print_fn=print_fn,
        )
        return

    if command == "/quit":
        if argument:
            notify_or_print(ui, "用法：/quit", print_fn=print_fn)
            return
        return ExitAction.QUIT

    if command in ("/help", "/h"):
        print_fn(SLASH_HELP)
        return

    if command == "/login":
        if ui is None:
            print_fn("请运行 pamu login，或在交互终端输入 /login。")
            return
        from .onboarding import login

        selected = login(ui, credentials_path)
        if selected and runtime is not None:
            handle_slash_command(
                runtime, f"/provider {selected}", credentials_path=credentials_path,
                client_factory=client_factory, print_fn=print_fn, ui=ui,
            )
        return selected

    if runtime is None:
        if command == "/status":
            _cmd_status([], credentials_path=credentials_path)
        elif command == "/model" and ui is not None:
            from .onboarding import choose_model

            data = auth.load_credentials(credentials_path)
            name = auth.configured_provider(data)
            if name:
                record = auth.get_login(data, name)
                model = argument or choose_model(ui, providers.get_provider(name), record.get("model"))
                if model:
                    try:
                        auth.store_login(name, record.get("api_key", ""), model, credentials_path)
                    except OSError:
                        notify_or_print(
                            ui,
                            "模型保存失败，请检查凭证文件的写入权限或磁盘空间。",
                            print_fn=print_fn,
                        )
                        return
                    return name
        notify_or_print(ui, LOGIN_HINT, print_fn=print_fn)
        return

    if command == "/status":
        provider, model, fallback = runtime.model.snapshot()
        coordinator = getattr(runtime, "sessions", None)
        print_fn(f"会话：{getattr(coordinator, 'session_id', None) or '尚未保存'}")
        print_fn(f"供应商：{provider}")
        print_fn(f"协议：{runtime.model.protocol}")
        if runtime.model.base_url:
            print_fn(f"服务地址：{runtime.model.base_url}")
        print_fn(f"模型：{model}")
        print_fn(f"模型来源：{runtime.model.model_source}")
        print_fn(f"降级模型：{fallback or '（无）'}")
        print_fn(f"思考程度：{runtime.model.thinking_snapshot()}")
        data = auth.load_credentials(credentials_path)
        if data.get("providers"):
            names = ", ".join(sorted(data["providers"]))
            print_fn(f"已登录供应商：{names}")
        return

    if command == "/model":
        if not argument:
            name, model, _ = runtime.model.snapshot()
            if ui is None:
                print_fn(f"当前模型：{model}")
                print_fn("用法：/model <model-id>")
                return
            from .onboarding import choose_model

            argument = choose_model(ui, providers.get_provider(name), model)
            if argument is None:
                notify_or_print(ui, "已取消模型选择。", print_fn=print_fn)
                return
        data = auth.load_credentials(credentials_path)
        active = runtime.model.provider
        if active and auth.get_login(data, active):
            try:
                auth.set_model(active, argument, credentials_path)
            except OSError:
                notify_or_print(
                    ui,
                    "模型保存失败，请检查凭证文件的写入权限或磁盘空间。",
                    print_fn=print_fn,
                )
                return
        runtime.model.switch_model(argument)
        LOGGER.info("switch model=%s provider=%s", argument, runtime.model.provider)
        print_fn(f"模型已切换为 {argument}（当前会话立即生效）。")
        return

    if command == "/thinking":
        level = runtime.model.thinking_snapshot()
        if not argument:
            if ui is None:
                print_fn(f"当前思考程度：{level}（{THINKING_LABELS[level]}）")
                print_fn("用法：/thinking <" + "|".join(THINKING_LEVELS) + ">")
                return
            argument = ui.choose(
                "思考程度",
                [(name, THINKING_LABELS[name]) for name in THINKING_LEVELS],
                initial=level,
            )
            if argument is None:
                notify_or_print(ui, "已取消思考程度选择。", print_fn=print_fn)
                return
        selected = normalize_level(argument)
        if selected is None:
            notify_or_print(
                ui,
                f"未知思考程度：{argument}。可用：{', '.join(THINKING_LEVELS)}",
                print_fn=print_fn,
            )
            return
        provider = runtime.model.provider
        if selected != "off" and providers.get_provider(provider).protocol != "anthropic_messages":
            notify_or_print(ui, "当前协议暂不支持 /thinking 五档设置；可使用 off。", print_fn=print_fn)
            return
        data = auth.load_credentials(credentials_path)
        if provider and auth.get_login(data, provider):
            try:
                auth.set_thinking(provider, selected, credentials_path)
            except OSError:
                notify_or_print(
                    ui,
                    "思考程度保存失败，请检查凭证文件的写入权限或磁盘空间。",
                    print_fn=print_fn,
                )
                return
        runtime.model.switch_thinking(selected)
        LOGGER.info("switch thinking=%s provider=%s", selected, provider)
        print_fn(f"思考程度已切换为 {selected}（{THINKING_LABELS[selected]}）。")
        return

    if command == "/provider":
        from .bootstrap import load_runtime_environment

        data = auth.load_credentials(credentials_path)
        environment = load_runtime_environment()
        if not argument:
            if ui is None:
                names = ", ".join(sorted(providers.PROVIDERS))
                print_fn(f"可用供应商：{names}")
                print_fn("用法：/provider <name>")
                return
            from .onboarding import choose_provider

            argument = choose_provider(ui, data, environment, runtime.model.provider)
            if argument is None:
                notify_or_print(ui, "已取消供应商切换。", print_fn=print_fn)
                return
        try:
            spec = providers.get_provider(argument)
        except providers.UnknownProviderError:
            notify_or_print(
                ui,
                f"未知供应商：{argument}。可用："
                f"{', '.join(sorted(providers.PROVIDERS))}",
                print_fn=print_fn,
            )
            return
        record = auth.get_login(data, spec.name)
        try:
            # An explicit session selection uses the target's saved model.
            switch_environment = dict(environment)
            if record.get("model"):
                switch_environment.pop("MODEL_ID", None)
            resolved = resolve_provider_config(switch_environment, data, spec.name)
        except ValueError as error:
            notify_or_print(
                ui,
                f"无法切换 {spec.label}：{error} 请先输入 /login 或运行 pamu login {spec.name}",
                print_fn=print_fn,
            )
            return
        from .cli_progress import terminal_progress

        providers.apply_provider(
            runtime,
            resolved.spec,
            resolved.api_key,
            resolved.model,
            anthropic_factory=client_factory,
            progress=terminal_progress,
            base_url=resolved.base_url,
            auth_token=resolved.auth_token,
            thinking=record.get("thinking"),
            model_source=resolved.model_source,
        )
        auth.set_active(spec.name, credentials_path)
        LOGGER.info("switch provider=%s", spec.name)
        model = resolved.model
        print_fn(f"供应商已切换为 {spec.label}（模型 {model}），并已设为默认。")
        return

    notify_or_print(ui, f"未知命令：{command}。输入 /help 查看可用命令。", print_fn=print_fn)


def _create_runtime(runtime_factory, credentials_path, session_selection=None):
    try:
        if runtime_factory is build_default_runtime:
            return runtime_factory(credentials_path=credentials_path, session_selection=session_selection)
        return runtime_factory()
    except MissingConfigurationError as error:
        global_config = Path.home() / ".pamu" / ".settings"
        project_config = Path.cwd().resolve() / ".pamu" / ".settings"
        print(
            f"配置错误：缺少必需配置项 {error.key}。\n"
            "请在以下任一位置添加配置：\n"
            f"  全局：{global_config}\n"
            f"  项目：{project_config}\n"
            "示例：MODEL_ID=claude-sonnet-4-6\n"
            "或运行 pamu login 配置模型服务。",
            file=sys.stderr,
        )
    except providers.MissingApiKeyError as error:
        print(f"登录信息缺失：{error}", file=sys.stderr)
    except ProviderConfigurationError as error:
        print(f"配置错误：{error}", file=sys.stderr)
    except SessionError as error:
        print(f"会话错误：{error}", file=sys.stderr)
    return None


def main(
    runtime_factory=build_default_runtime,
    run_turn=run_agent_turn,
    start_threads=start_runtime_threads,
    stop_threads=stop_runtime_threads,
    argv: list[str] | None = None,
    *,
    credentials_path: Path | None = None,
    verify=providers.verify_login,
    client_factory=None,
    ui=None,
    configuration_check=None,
) -> int | None:
    args = list(sys.argv[1:]) if argv is None else list(argv)
    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    if ui is None and interactive:
        from .terminal import TerminalUI

        ui = TerminalUI(workdir=Path.cwd())
    if args and args[0] in COMMANDS:
        if args[0] == "login" and ui is not None:
            from .onboarding import login

            rest = [arg for arg in args[1:] if arg != "--no-verify"]
            try:
                selected = login(ui, credentials_path or auth.CREDENTIALS_PATH, rest[0] if rest else None)
            except (EOFError, KeyboardInterrupt):
                return 2
            except providers.UnknownProviderError:
                print(f"未知供应商：{rest[0]}。可用：{', '.join(providers.PROVIDERS)}", file=sys.stderr)
                return 2
            return 0 if selected else 2
        return run_command(
            args, credentials_path=credentials_path, verify=verify,
            client_factory=client_factory,
        )
    selected_session = None
    if args and args[0] == "resume":
        from .session_cli import select_session
        try:
            selected_session = select_session(args[1:], ui)
        except (EOFError, KeyboardInterrupt, QuitRequested):
            return 0
        except SessionError as error:
            print(f"会话错误：{error}", file=sys.stderr)
            return 2
        if selected_session is None:
            return 0
    if configuration_check is None:
        from .bootstrap import has_local_configuration

        configuration_check = (
            (lambda: has_local_configuration(credentials_path))
            if runtime_factory is build_default_runtime else lambda: True
        )
    runtime = None
    configuration_error = False
    if selected_session is not None or ui is None or configuration_check():
        try:
            runtime = _create_runtime(runtime_factory, credentials_path, selected_session[0]["selection"] if selected_session else None)
        except BaseException:
            if selected_session:
                selected_session[1].close()
            raise
        if runtime is None:
            configuration_error = True
            if selected_session:
                selected_session[1].close()
                return 2
            if not interactive:
                return 2
    if selected_session:
        coordinator = getattr(runtime, "sessions", None)
        try:
            if coordinator is None:
                raise SessionError("当前运行时不支持会话恢复")
            coordinator.restore(runtime, *selected_session)
            from .session_cli import show_restored_session
            show_restored_session(selected_session[0], runtime)
        except BaseException as error:
            if coordinator is not None:
                coordinator.close()
            selected_session[1].close()
            if isinstance(error, SessionError):
                print(f"会话错误：{error}", file=sys.stderr)
                return 2
            raise
    exit_code = None
    threads = []
    exit_gesture = ExitGesture()
    if ui is not None:
        ui.repl_mode = True
    try:
        if interactive:
            if runtime is not None and hasattr(runtime, "model"):
                provider, model = runtime.model.snapshot()[:2]
            else:
                provider, model = None, None
            try:
                print(
                    render_startup_screen(
                        cwd=str(Path.cwd()),
                        provider=provider,
                        model=model,
                        configuration_error=configuration_error,
                        color=(
                            "NO_COLOR" not in os.environ
                            and os.environ.get("TERM") != "dumb"
                        ),
                        width=shutil.get_terminal_size(fallback=(80, 24)).columns,
                    )
                )
            except (OSError, ValueError):
                print("PamuCode · 输入 /help 查看命令")
        if runtime is not None:
            threads = start_threads(runtime)
        if runtime is None:
            notify_or_print(ui, LOGIN_HINT)
        while True:
            if ui is not None and runtime is not None:
                _provider, ui.model_name = runtime.model.snapshot()[:2]
                ui.thinking_level = runtime.model.thinking_snapshot()
            try:
                # Non-interactive fallback reads a line without a prompt marker.
                query = ui.prompt() if ui is not None else input("")
            except EOFError:
                break
            except QuitRequested:
                print("Shutting down PamuCode…")
                break
            except KeyboardInterrupt:
                if ui is not None:
                    continue
                if exit_gesture.press():
                    print("Shutting down PamuCode…")
                    break
                print("再次按 Ctrl+C 退出 PamuCode")
                continue
            if not query.strip():
                continue
            if query.strip().startswith("/"):
                try:
                    selected = handle_slash_command(
                        runtime, query, credentials_path=credentials_path,
                        client_factory=client_factory, ui=ui,
                    )
                except QuitRequested:
                    print("Shutting down PamuCode…")
                    break
                except (EOFError, KeyboardInterrupt):
                    continue
                if selected is ExitAction.QUIT:
                    print("Shutting down PamuCode…")
                    break
                if runtime is None and selected:
                    runtime = _create_runtime(runtime_factory, credentials_path)
                    if runtime is None:
                        notify_or_print(ui, LOGIN_HINT)
                    else:
                        threads = start_threads(runtime)
                continue
            if runtime is None:
                notify_or_print(ui, LOGIN_HINT)
                continue
            if ui is not None:
                print(query)
            runtime.hooks.trigger("UserPromptSubmit", query)
            with runtime.agent_lock:
                run_turn(runtime, query)
    except SessionError as error:
        print(f"会话错误：{error}", file=sys.stderr)
        exit_code = 2
    finally:
        if runtime is not None:
            coordinator = getattr(runtime, "sessions", None)
            try:
                stop_threads(runtime, threads)
                if coordinator is not None and exit_code != 2:
                    lock = runtime.agent_lock
                    acquire = getattr(lock, "acquire", None)
                    # Legacy injected locks may only support context management.
                    # Without nonblocking acquisition there is no safe final snapshot.
                    if acquire is not None and acquire(blocking=False):
                        try:
                            checkpoint(runtime)
                        finally:
                            lock.release()
            except SessionError as error:
                print(f"会话错误：{error}", file=sys.stderr)
                exit_code = 2
            finally:
                if coordinator is not None:
                    coordinator.close()
    return exit_code
