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
from . import theme
from .bootstrap import build_default_runtime
from .branding import render_startup_screen
from .cli_exit import ExitAction, ExitGesture, QuitRequested
from .cli_progress import write_user_message
from .config import MissingConfigurationError
from .core.interruption import protect_interrupt_cleanup
from .core.context_manager import estimate_current_context
from .core.loop import run_agent_loop
from .features.background import (
    close_background_tasks, collect_background_results, describe_background_tasks, stop_background_tasks,
)
from .features.dreaming import maybe_dream_on_exit
from .features.sessions import checkpoint, interrupt_turn
from .features.session_store import SessionError
from .features import auth, skills
from .features.skills import SkillInvocation
from .features.scheduler import (
    cancel_job,
    cron_scheduler_loop,
    describe_job,
    has_cron_queue,
    list_jobs,
    load_durable_jobs,
    queued_jobs,
)
from .rendering import help_block
from .repl_sessions import ResumeAction, delete_current_session, resume_runtime
from .runtime import RuntimeContext
from .logging_setup import get_logger
from .slash_commands import SLASH_COMMANDS, help_text
from .thinking import THINKING_LABELS, THINKING_LEVELS, normalize_level

LOGGER = get_logger("pamu.cli")
COMMANDS = ("login", "logout", "status", "use")

SLASH_HELP = help_text()
LOGIN_HINT = "尚未配置模型服务，请输入 /login 选择供应商、服务地址和模型。"
SHUTDOWN = "Shutting down PamuCode…"
UNDO_SCOPE = "（/undo 只覆盖 write_file/edit_file 的修改，Shell 命令造成的改动不会被撤销。）"


def notify_or_print(ui, message: str, *, print_fn=print) -> None:
    notify = getattr(ui, "notify", None)
    if callable(notify):
        notify(message)
    else:
        print_fn(message)


def offer_project_skill_trust(runtime, ui) -> None:
    """Ask once per skill set whether the workspace's own skills may load."""
    state = getattr(runtime, "skills", None)
    if state is None or getattr(runtime.config, "sandbox_mode", "off") == "confined":
        return
    skills.refresh_skills(state)
    if not state.pending or state.asked_fingerprint == state.pending_fingerprint:
        return
    state.asked_fingerprint = state.pending_fingerprint
    choice = ui.choose(
        f"此项目自带 {len(state.pending)} 个技能：{skills.describe_pending(state)}。"
        "它们会进入系统提示词并可被模型加载，是否信任？",
        [("no", "暂不信任（之后可输入 /skill trust）"), ("yes", "信任此项目的技能")],
    )
    if choice == "yes":
        names = skills.trust_project_skills(state)
        print(theme.success(f"已信任此项目的技能：{', '.join(names)}"))


def _save_background_results(runtime):
    notifications = collect_background_results(runtime.background)
    runtime.session.history.extend(
        {'role': 'user', '_pamu_source': 'system', 'content': text} for text in notifications
    )
    checkpoint(runtime)


def run_agent_turn(runtime, user_query: str | None = None) -> None:
    # Slash commands are REPL-local controls; they never join the LLM context.
    if user_query and not user_query.lstrip().startswith("/"):
        from datetime import datetime
        from uuid import uuid4
        if hasattr(runtime.session, 'context_state'):
            runtime.session.context_state['current_request'] = {'id': str(uuid4()), 'text': user_query}
            runtime.session.context_state['turn_time'] = datetime.now().isoformat(timespec='seconds')
        runtime.session.history.append({"role": "user", "content": user_query})
    try:
        checkpoint(runtime, status="running")
        run_agent_loop(runtime)
    except KeyboardInterrupt:
        with protect_interrupt_cleanup():
            interrupt_turn(runtime)
        raise
    except BaseException:
        coordinator = getattr(runtime, "sessions", None)
        if coordinator is None or not coordinator.failed:
            checkpoint(runtime, status="interrupted")
        raise
    else:
        checkpoint(runtime, status="idle")
    print()


def _run_interactive_turn(runtime, query, run_turn, ui):
    started = False
    try:
        with runtime.agent_lock:
            started = True
            runtime.hooks.trigger("UserPromptSubmit", query)
            run_turn(runtime, query)
    except KeyboardInterrupt:
        if not started:
            notify_or_print(ui, "已取消等待，正在占用会话的任务仍在运行。")
            return
        # run_agent_turn has saved the interrupted history before control returns.
        # Keep the same runtime/lease; this is not a resume or a new conversation.
        notify_or_print(ui, "已中断当前轮次，已完成的修改保留。可继续输入指令。")
        background = getattr(runtime, 'background', None)
        if background is not None:
            with background.lock:
                running = [key for key, task in background.tasks.items() if task['status'] == 'running']
            if running:
                print(f"后台命令仍在运行：{', '.join(running)}。用 /background 查看，/stop <编号> 停止。")
        team = getattr(runtime, 'team', None)
        if team is not None:
            with team.lock:
                if team.active:
                    print("队友仍独立运行；当前轮次的中断不会停止队友。")


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
                LOGGER.debug("delivering scheduled work")
                names = ", ".join(job.id for job in queued_jobs(runtime.scheduler))
                print(theme.paint(f"⏰ 定时任务 {names} 开始执行；需要确认的操作会被自动拒绝。", "muted"))
                run_agent_turn(runtime)
        except SessionError as exc:
            print(str(exc))
            runtime.stop_event.set()
            return
        finally:
            runtime.agent_lock.release()


def start_runtime_threads(runtime) -> list[threading.Thread]:
    if load_durable_jobs(runtime.scheduler, runtime.config):
        print(f"已加载 {len(list_jobs(runtime.scheduler))} 个持久定时任务，输入 /cron 查看或取消。")
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
    except BaseException as error:
        error.threads = started
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
    mutating = query.strip().partition(" ")[0] in ("/model", "/provider", "/thinking", "/context", "/compact", "/skill", "/undo")
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
) -> str | ExitAction | SkillInvocation | ResumeAction | None:
    """Handle one slash command inside the REPL, mutating runtime on switch."""
    credentials_path = credentials_path or auth.CREDENTIALS_PATH
    command, _, rest = query.strip().partition(" ")
    argument = rest.strip()
    if command == '/undo':
        if runtime is None:
            notify_or_print(ui, LOGIN_HINT, print_fn=print_fn)
            return
        checkpoints = getattr(runtime, 'checkpoints', None)
        if checkpoints is None:
            notify_or_print(ui, '当前沙箱模式不支持 /undo。', print_fn=print_fn)
            return
        turn = checkpoints.latest()
        if turn is None:
            notify_or_print(ui, f'没有可撤销的文件修改。{UNDO_SCOPE}', print_fn=print_fn)
            return
        files, conflicts = checkpoints.files(turn), checkpoints.conflicts(turn)
        if ui is None:
            print_fn('上一轮修改的文件：' + '、'.join(files) + '\n请在交互终端使用 /undo 并确认。')
            return
        title = f"撤销上一轮对 {len(files)} 个文件的修改：{'、'.join(files)}"
        if conflicts:
            title += f"\n注意：{'、'.join(conflicts)} 在那之后又被修改过，撤销会覆盖这些修改。"
        if ui.choose(title, [('cancel', '取消'), ('undo', '撤销并覆盖' if conflicts else '撤销')]) != 'undo':
            notify_or_print(ui, '已取消撤销。', print_fn=print_fn)
            return
        restored = checkpoints.restore(turn)
        runtime.session.history.append({'role': 'user', '_pamu_source': 'system', 'content':
                                        '[用户执行了 /undo] 以下文件已恢复到上一轮修改之前：' + '、'.join(restored)})
        checkpoint(runtime)
        print_fn(theme.success('已撤销：' + '、'.join(restored)) + f'\n{UNDO_SCOPE}')
        return
    if command == '/memory':
        if runtime is None:
            notify_or_print(ui, LOGIN_HINT, print_fn=print_fn)
            return
        from .features.memory import forget_note, list_notes
        action, _, target = argument.partition(' ')
        if not action:
            notes = list_notes(runtime.memory)
            index = runtime.memory.index_path
            lines = [] if getattr(runtime.config, 'auto_memory', True) else [
                '（PAMU_AUTO_MEMORY=off：模型只读记忆，退出时不自动整理）']
            has_index = index.is_file() and not index.is_symlink()
            if has_index:
                count = len(index.read_text(encoding='utf-8', errors='replace').splitlines())
                lines.append(f"MEMORY.md  {count} 行（每个会话加载前 200 行）")
            lines += [f"{note['path']}  {note['summary']}".rstrip() for note in notes]
            print_fn('\n'.join(lines if has_index or notes else lines + ['没有记忆。']))
        elif action == 'forget' and target.strip():
            path = forget_note(runtime.memory, target)
            notify_or_print(ui, f"已删除记忆：{path}" if path else f"记忆不存在：{target.strip()}", print_fn=print_fn)
        else:
            notify_or_print(ui, '用法：/memory 或 /memory forget <路径>', print_fn=print_fn)
        return
    if command == '/dream':
        if runtime is None:
            notify_or_print(ui, LOGIN_HINT, print_fn=print_fn)
            return
        from .features.dreaming import dream_available, run_dream
        reason = dream_available(runtime)
        if reason:
            notify_or_print(ui, reason, print_fn=print_fn)
            return
        if not runtime.agent_lock.acquire(blocking=False):
            notify_or_print(ui, '代理正在执行任务，结束后再整理记忆。', print_fn=print_fn)
            return
        try:
            print_fn(theme.paint('正在整理记忆（Ctrl+C 中断）…', 'muted'))
            report = run_dream(runtime)
            print_fn(report.describe() + (f"\n{report.summary}" if report.summary else ''))
        except KeyboardInterrupt:
            notify_or_print(ui, '已中断记忆整理，记忆保持原样。', print_fn=print_fn)
        except Exception as error:
            LOGGER.warning('dream failed', exc_info=True)
            notify_or_print(ui, f'记忆整理失败，记忆保持原样：{error}', print_fn=print_fn)
        finally:
            runtime.agent_lock.release()
        return
    if command == '/cron':
        if runtime is None:
            notify_or_print(ui, LOGIN_HINT, print_fn=print_fn)
            return
        action, _, target = argument.partition(' ')
        target = target.strip()
        if not action:
            jobs = list_jobs(runtime.scheduler)
            print_fn('\n'.join(f"{job.id}  '{job.cron}'  {describe_job(job)}  → {job.prompt[:60]}" for job in jobs)
                     or '没有定时任务。')
        elif action == 'cancel' and target:
            ids = [job.id for job in list_jobs(runtime.scheduler)] if target == 'all' else [target]
            print_fn('\n'.join(cancel_job(runtime.scheduler, runtime.config, job_id) for job_id in ids)
                     or '没有定时任务。')
        else:
            notify_or_print(ui, '用法：/cron 或 /cron cancel <任务编号 | all>', print_fn=print_fn)
        return
    if command in ('/background', '/stop'):
        if runtime is None:
            notify_or_print(ui, LOGIN_HINT, print_fn=print_fn)
            return
        if command == '/background':
            print_fn(describe_background_tasks(runtime.background))
        elif not argument:
            print_fn('用法：/stop <后台任务编号 | all>。用 /background 查看编号。')
        else:
            try:
                with protect_interrupt_cleanup():
                    outcomes = stop_background_tasks(runtime.background, argument)
                    # Cancellation must work even when a scheduled turn owns history.
                    # In that case the turn will collect the notification itself.
                    if runtime.agent_lock.acquire(blocking=False):
                        try:
                            _save_background_results(runtime)
                        finally:
                            runtime.agent_lock.release()
                labels = {'running': '正在停止，尚未确认结束', 'cancelled': '已中断，已发生的修改保留',
                          'completed': '已完成', 'failed': '失败'}
                print_fn('\n'.join(f'{key}：{labels[value]}' for key, value in outcomes.items()) or '没有后台命令。')
            except ValueError as error:
                notify_or_print(ui, str(error), print_fn=print_fn)
        return
    if command == "/skill":
        if runtime is None:
            notify_or_print(ui, LOGIN_HINT, print_fn=print_fn)
            return
        if runtime.config.sandbox_mode == "confined":
            notify_or_print(ui, "当前沙箱模式不支持加载技能。", print_fn=print_fn)
            return
        catalog = skills.refresh_skills(runtime.skills)
        if argument == "trust":
            names = skills.trust_project_skills(runtime.skills)
            notify_or_print(
                ui, theme.success(f"已信任此项目的技能：{', '.join(names)}") if names else "没有等待信任的项目技能。",
                print_fn=print_fn,
            )
            return
        if runtime.skills.pending:
            notify_or_print(ui, f"此项目自带的技能尚未信任：{skills.describe_pending(runtime.skills)}。"
                                "输入 /skill trust 加载。", print_fn=print_fn)
        if not catalog:
            places = "、".join(skills.root_label(runtime.skills, root) for root in runtime.skills.roots)
            notify_or_print(ui, f"没有可用技能。可在以下目录添加 <名称>/SKILL.md，无需重启：{places}", print_fn=print_fn)
            return
        parts = argument.split(maxsplit=1)
        name = parts[0] if parts else None
        task = parts[1] if len(parts) > 1 else ""
        if name is None:
            if ui is None:
                print_fn("可用技能：\n" + "\n".join(
                    f"  {skills.describe_skill(runtime.skills, key)}" for key in sorted(catalog)
                ) + "\n使用 /skill <名称> [任务描述] 调用。")
                return
            name = ui.choose("选择技能", [
                (key, skills.describe_skill(runtime.skills, key)) for key in sorted(catalog)
            ])
            if name is None:
                notify_or_print(ui, "已取消技能调用。", print_fn=print_fn)
                return
        key = skills.resolve_skill(runtime.skills, name)
        if key is None:
            notify_or_print(ui, f"技能不存在：{name}。输入 /skill 查看可用技能。", print_fn=print_fn)
            return
        name = key
        content = runtime.tools.snapshot()[1]["load_skill"](name=name)
        checkpoint(runtime)
        print_fn(f"已调用技能：{name}" + (f" · {task}" if task else ""))
        return SkillInvocation(name, content, task)
    if command in {"/plan", "/execute"}:
        if runtime is None:
            notify_or_print(ui, LOGIN_HINT, print_fn=print_fn)
        elif argument:
            notify_or_print(ui, f"用法：{command}", print_fn=print_fn)
        else:
            from .working_mode import switch_mode
            try:
                message = switch_mode(runtime, command[1:])
                print_fn(theme.success(message))
            except ValueError as exc:
                notify_or_print(ui, str(exc), print_fn=print_fn)
        return
    if command in ('/context', '/compact'):
        if runtime is None:
            notify_or_print(ui, LOGIN_HINT, print_fn=print_fn)
            return
        from .core.context_manager import compact, context_control
        try:
            if command == '/context':
                message = context_control(runtime, argument)
            else:
                tools, _ = runtime.tools.snapshot()
                from .core.context import build_context
                system = runtime.prompt_builder.build(build_context(runtime, tools))
                message = '上下文压缩完成。' if compact(runtime, focus=argument, system=system, tools=tools) else '没有可压缩的旧历史。'
            notify_or_print(ui, message, print_fn=print_fn)
        except SessionError:
            raise
        except ValueError as exc:
            notify_or_print(ui, f'上下文操作失败：{exc}', print_fn=print_fn)
        return

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

    if command == "/resume":
        return ResumeAction(tuple(argument.split()))

    if command == "/delete":
        if argument:
            notify_or_print(ui, "用法：/delete", print_fn=print_fn)
            return
        coordinator = getattr(runtime, "sessions", None)
        if coordinator is None or coordinator.session_id is None:
            notify_or_print(ui, "当前没有已保存的会话。", print_fn=print_fn)
            return
        if ui is None:
            notify_or_print(ui, "请在交互终端使用 /delete 并确认删除。", print_fn=print_fn)
            return
        choice = ui.choose(
            "永久删除当前会话并退出？",
            [("cancel", "取消"), ("delete", "删除并退出")],
        )
        if choice == "delete":
            return ExitAction.DELETE
        notify_or_print(ui, "已取消删除会话。", print_fn=print_fn)
        return

    if command == "/quit":
        if argument:
            notify_or_print(ui, "用法：/quit", print_fn=print_fn)
            return
        return ExitAction.QUIT

    if command in ("/help", "/h"):
        if theme.rich_output():
            print_fn(help_block(SLASH_COMMANDS))
        else:
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
            from .bootstrap import load_runtime_environment
            from .onboarding import choose_any_model

            data = auth.load_credentials(credentials_path)
            environment = load_runtime_environment()
            name = auth.configured_provider(data)
            if name:
                saved = auth.get_login(data, name).get("model")
                if argument:
                    selected = (_model_provider(argument, data, environment, name, saved), argument)
                else:
                    selected = choose_any_model(ui, data, environment, name, saved)
                if selected is None:
                    notify_or_print(ui, "已取消模型选择。", print_fn=print_fn)
                    return
                name, model = selected
                try:
                    auth.store_login(name, auth.get_login(data, name).get("api_key", ""), model, credentials_path)
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
        name, model, _ = runtime.model.snapshot()
        if not argument and ui is None:
            print_fn(f"当前模型：{model}")
            print_fn("用法：/model <model-id>")
            return
        from .bootstrap import load_runtime_environment
        from .onboarding import choose_any_model

        data = auth.load_credentials(credentials_path)
        environment = load_runtime_environment()
        if argument:
            target = _model_provider(argument, data, environment, name, model)
        else:
            selected = choose_any_model(ui, data, environment, name, model)
            if selected is None:
                notify_or_print(ui, "已取消模型选择。", print_fn=print_fn)
                return
            target, argument = selected
        if target != name:
            resolved = _switch_provider(
                runtime, target, data, environment, model=argument, credentials_path=credentials_path,
                client_factory=client_factory, print_fn=print_fn, ui=ui,
            )
            if resolved is not None:
                print_fn(theme.success(
                    f"模型已切换为 {resolved.spec.label} · {argument}（当前会话立即生效），并已设为默认供应商。"
                ))
            return
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
        print_fn(theme.success(f"模型已切换为 {argument}（当前会话立即生效）。"))
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
        print_fn(theme.success(f"思考程度已切换为 {selected}（{THINKING_LABELS[selected]}）。"))
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
        resolved = _switch_provider(
            runtime, spec.name, data, environment, credentials_path=credentials_path,
            client_factory=client_factory, print_fn=print_fn, ui=ui,
        )
        if resolved is not None:
            print_fn(theme.success(f"供应商已切换为 {spec.label}（模型 {resolved.model}），并已设为默认。"))
        return

    notify_or_print(ui, f"未知命令：{command}。输入 /help 查看可用命令。", print_fn=print_fn)


def _model_provider(model, credentials, environment, current, current_model):
    """Keep the current provider unless only another configured provider offers ``model``."""
    from .onboarding import model_catalog

    owners = [name for name, item in model_catalog(credentials, environment, current, current_model) if item == model]
    return current if not owners or current in owners else owners[0]


def _switch_provider(runtime, name, data, environment, *, model=None, credentials_path,
                     client_factory, print_fn, ui):
    """Rebuild the live adapter for ``name``; ``model`` overrides its saved model."""
    spec = providers.get_provider(name)
    record = auth.get_login(data, spec.name)
    try:
        # An explicit session selection uses the target's saved model.
        switch_environment = dict(environment)
        if model:
            switch_environment["MODEL_ID"] = model
        elif record.get("model"):
            switch_environment.pop("MODEL_ID", None)
        resolved = resolve_provider_config(switch_environment, data, spec.name)
    except ValueError as error:
        notify_or_print(
            ui,
            f"无法切换 {spec.label}：{error} 请先输入 /login 或运行 pamu login {spec.name}",
            print_fn=print_fn,
        )
        return None
    if model and record:
        try:
            auth.set_model(spec.name, model, credentials_path)
        except OSError:
            notify_or_print(ui, "模型保存失败，请检查凭证文件的写入权限或磁盘空间。", print_fn=print_fn)
            return None
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
        model_source="当前会话选择" if model else resolved.model_source,
    )
    auth.set_active(spec.name, credentials_path)
    LOGGER.info("switch provider=%s model=%s", spec.name, resolved.model)
    return resolved


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
    dream_on_exit=maybe_dream_on_exit,
) -> int | None:
    args = list(sys.argv[1:]) if argv is None else list(argv)
    if args and args[0] == "run":
        from .execution import main as run_headless
        return run_headless(args[1:])
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
    if args and args[0] in {"resume", "-r", "continue", "-c"}:
        from .session_cli import select_session
        try:
            selected_session = select_session(args[1:], ui, latest=args[0] in {"continue", "-c"})
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
    if selected_session is not None or ui is None or configuration_check():
        try:
            runtime = _create_runtime(runtime_factory, credentials_path, selected_session[0]["selection"] if selected_session else None)
        except BaseException:
            if selected_session:
                selected_session[1].close()
            raise
        if runtime is None:
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
    save_on_exit = True
    threads = []
    exit_gesture = ExitGesture()
    if ui is not None:
        ui.repl_mode = True
    try:
        if interactive and selected_session is None:
            try:
                print(
                    render_startup_screen(
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
                offer_project_skill_trust(runtime, ui)
                _provider, ui.model_name = runtime.model.snapshot()[:2]
                ui.thinking_level = runtime.model.thinking_snapshot()
                from .working_mode import working_mode
                ui.working_mode = working_mode(getattr(runtime, "session", None))
                if isinstance(runtime, RuntimeContext):
                    ui.context_used = None
                    if runtime.agent_lock.acquire(blocking=False):
                        try:
                            report = estimate_current_context(runtime)
                            ui.context_used = round(100 * report.input_tokens / report.budget.window)
                        except (OSError, ValueError):
                            # A status estimate must not stop the interactive session.
                            LOGGER.debug("Could not estimate context usage for the prompt")
                        finally:
                            runtime.agent_lock.release()
            try:
                # Non-interactive fallback reads a line without a prompt marker.
                query = ui.prompt() if ui is not None else input("")
            except EOFError:
                break
            except QuitRequested:
                print(theme.paint(SHUTDOWN, "muted"))
                break
            except KeyboardInterrupt:
                if ui is not None:
                    continue
                if exit_gesture.press():
                    print(theme.paint(SHUTDOWN, "muted"))
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
                    print(theme.paint(SHUTDOWN, "muted"))
                    break
                except (EOFError, KeyboardInterrupt):
                    continue
                if selected is ExitAction.QUIT:
                    print(theme.paint(SHUTDOWN, "muted"))
                    break
                if selected is ExitAction.DELETE:
                    with protect_interrupt_cleanup():
                        reason = delete_current_session(runtime)
                        if not reason:
                            save_on_exit = False
                    if reason:
                        notify_or_print(ui, reason)
                        continue
                    print("已删除当前会话。")
                    break
                if isinstance(selected, SkillInvocation):
                    _run_interactive_turn(runtime, selected.prompt, run_turn, ui)
                    continue
                if isinstance(selected, ResumeAction):
                    if runtime is None:
                        notify_or_print(ui, LOGIN_HINT)
                    else:
                        runtime, threads = resume_runtime(
                            runtime, threads, selected,
                            create_runtime=lambda selection: _create_runtime(runtime_factory, credentials_path, selection),
                            start_threads=start_threads, stop_threads=stop_threads, ui=ui,
                        )
                    continue
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
                write_user_message(query)
            _run_interactive_turn(runtime, query, run_turn, ui)
    except SessionError as error:
        print(f"会话错误：{error}", file=sys.stderr)
        exit_code = 2
    finally:
        if runtime is not None:
            coordinator = getattr(runtime, "sessions", None)
            try:
                stop_threads(runtime, threads)
                background = getattr(runtime, 'background', None)
                if background is not None:
                    with protect_interrupt_cleanup():
                        outcomes = close_background_tasks(background)
                    remaining = [key for key, status in outcomes.items() if status == 'running']
                    if remaining:
                        print(f"仍未确认停止的后台命令：{', '.join(remaining)}，请检查实际状态。")
                if coordinator is not None and exit_code != 2 and save_on_exit:
                    lock = runtime.agent_lock
                    acquire = getattr(lock, "acquire", None)
                    # Legacy injected locks may only support context management.
                    # Without nonblocking acquisition there is no safe final snapshot.
                    if acquire is not None and acquire(blocking=False):
                        try:
                            if background is not None:
                                _save_background_results(runtime)
                            else:
                                checkpoint(runtime)
                        finally:
                            lock.release()
                if ui is not None and isinstance(runtime, RuntimeContext) and exit_code != 2 and save_on_exit:
                    dream_on_exit(runtime)
            except SessionError as error:
                print(f"会话错误：{error}", file=sys.stderr)
                exit_code = 2
            finally:
                if coordinator is not None:
                    coordinator.close()
    return exit_code
