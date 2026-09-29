"""Application composition root with no import-time runtime side effects."""

from __future__ import annotations

import os
import stat
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict
from pathlib import Path

from .adapters.anthropic import AnthropicAdapter
from .cli_progress import terminal_progress
from .config import AppConfig
from .core.prompt import PromptBuilder
from .core.recovery import RecoveryState, with_retry
from .features import auth as auth_store
from .features import (
    background,
    mcp,
    memory,
    scheduler,
    skills,
    subagents,
    tasks,
    todos,
    worktrees,
)
from .features.teams import bus as team_bus
from .features.teams import protocol as team_protocol
from .features.teams import teammates
from .logging_setup import setup_logging
from .runtime import ModelRouter, RuntimeContext, SessionState
from .thinking import DEFAULT_THINKING_LEVEL, normalize_level
from .tools import builtin
from .tools.hooks import (
    HookRegistry,
    make_context_inject_hook,
    make_diff_preview_hook,
    make_large_output_hook,
    make_log_hook,
    make_permission_hook,
    make_summary_hook,
)
from .tools.registry import ToolRegistry

INSTALL_ROOT = Path(__file__).resolve().parents[1]
GLOBAL_SETTINGS_PATH = Path.home() / ".pamu" / ".settings"
CREDENTIALS_PATH = auth_store.CREDENTIALS_PATH
TEAM_GUARDED_TOOLS = {"bash", "write_file"}


def _load_environment(
    workspace: Path,
    global_settings: Path,
    process_environment: Mapping[str, str],
    read: Callable[[Path], Mapping[str, str | None]],
) -> dict[str, str]:
    """Build isolated workspace settings without mutating the process."""

    def defined(values: Mapping[str, str | None]) -> dict[str, str]:
        return {name: value for name, value in values.items() if value is not None}

    project_values = defined(read(workspace / ".pamu" / ".settings"))
    global_values = defined(read(global_settings))
    return {
        **global_values,
        **project_values,
        **dict(process_environment),
    }


def _state_paths(config: AppConfig) -> tuple[Path, ...]:
    return (
        config.state_dir,
        config.state_dir / ".gitignore",
        config.memory_dir,
        config.memory_index,
        config.transcripts_dir,
        config.tool_result_dir,
        config.task_dir,
        config.mailbox_dir,
        config.scheduled_tasks_path,
        config.worktrees_dir,
        config.logs_dir,
    )


def _validate_state_paths(config: AppConfig) -> None:
    """Reject state paths that escape the workspace or traverse symlinks."""
    workspace = config.workdir.resolve()
    expected_state_dir = workspace / ".pamu"
    expected_paths = (
        expected_state_dir,
        expected_state_dir / ".gitignore",
        expected_state_dir / "memory",
        expected_state_dir / "memory" / "MEMORY.md",
        expected_state_dir / "transcripts",
        expected_state_dir / "task_outputs" / "tool-results",
        expected_state_dir / "tasks",
        expected_state_dir / "mailboxes",
        expected_state_dir / "scheduled_tasks.json",
        expected_state_dir / "worktrees",
        expected_state_dir / "logs",
    )

    for path, expected in zip(_state_paths(config), expected_paths, strict=True):
        candidate = path.absolute()
        if candidate != expected:
            raise ValueError(
                f"PamuCode state path does not match the workspace layout: "
                f"{candidate}"
            )

        component = workspace
        for part in expected.relative_to(workspace).parts:
            component /= part
            try:
                mode = component.lstat().st_mode
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(mode):
                raise ValueError(
                    f"PamuCode state path contains a symlink: {component}"
                )

    _validate_existing_gitignore(expected_state_dir / ".gitignore")


def _validate_existing_gitignore(ignore: Path) -> None:
    try:
        mode = ignore.lstat().st_mode
    except FileNotFoundError:
        return
    if not stat.S_ISREG(mode):
        kind = "symlink" if stat.S_ISLNK(mode) else "non-regular file"
        raise ValueError(
            f"PamuCode state .gitignore must be a regular file, not a "
            f"{kind}: {ignore}"
        )


def _create_state_gitignore(ignore: Path) -> None:
    try:
        handle = ignore.open("x", encoding="utf-8")
    except FileExistsError:
        _validate_existing_gitignore(ignore)
        return

    try:
        with handle:
            handle.write("*\n!.gitignore\n")
    except BaseException as error:
        try:
            ignore.unlink()
        except FileNotFoundError:
            pass
        except OSError as cleanup_error:
            error.add_note(
                f"Could not remove incomplete state .gitignore: "
                f"{cleanup_error}"
            )
        raise


def _create_storage_roots(config: AppConfig) -> None:
    _validate_state_paths(config)
    config.workdir.mkdir(parents=True, exist_ok=True)
    config.state_dir.mkdir(parents=True, exist_ok=True)
    _create_state_gitignore(config.state_dir / ".gitignore")
    for root in (
        config.memory_dir,
        config.transcripts_dir,
        config.tool_result_dir,
        config.task_dir,
        config.mailbox_dir,
        config.worktrees_dir,
        config.scheduled_tasks_path.parent,
        config.logs_dir,
    ):
        root.mkdir(parents=True, exist_ok=True)


def _run_git(config: AppConfig, args: list[str]) -> tuple[bool, str]:
    try:
        result = subprocess.run(  # noqa: S603 - fixed git argv, no user shell
            ["git", *args],  # noqa: S607 - git is resolved from PATH on purpose
            cwd=config.workdir,
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = (result.stdout + result.stderr).strip()
        return result.returncode == 0, output[:5000] if output else "(no output)"
    except subprocess.TimeoutExpired:
        return False, "Error: git timedout"


def _register_default_hooks(
    hooks: HookRegistry, config: AppConfig, mcp_state: mcp.MCPState
) -> None:
    hooks.register("UserPromptSubmit", make_context_inject_hook(config.workdir))
    hooks.register(
        "PreToolUse",
        make_permission_hook(config.workdir, input, mcp_state=mcp_state),
    )
    hooks.register("PreToolUse", make_log_hook(config.workdir))
    hooks.register("PreToolUse", make_diff_preview_hook(config.workdir, input))
    hooks.register("PostToolUse", make_large_output_hook(config.workdir))
    hooks.register("Stop", make_summary_hook(config.workdir))


def build_runtime(
    config: AppConfig,
    sdk_client,
    *,
    progress_factory=None,
    provider: str = "anthropic",
    thinking_level: str = DEFAULT_THINKING_LEVEL,
) -> RuntimeContext:
    """Build one completely independent runtime from explicit dependencies."""
    _create_storage_roots(config)
    setup_logging(config.logs_dir, config.log_level)
    llm = (
        AnthropicAdapter(sdk_client, progress_factory)
        if progress_factory is not None
        else AnthropicAdapter(sdk_client)
    )
    model_router = ModelRouter(
        provider=provider,
        primary_model=config.primary_model,
        fallback_model=config.fallback_model,
        llm=llm,
        thinking_level=normalize_level(thinking_level) or DEFAULT_THINKING_LEVEL,
    )

    session = SessionState()
    scheduler_state = scheduler.SchedulerState()
    background_state = background.BackgroundState()
    task_store = tasks.TaskStore(config.task_dir)
    skill_state = skills.SkillState(config.skills_dir)
    memory_store = memory.MemoryStore(config.memory_dir, config.memory_index)
    message_bus = team_bus.MessageBus(config.mailbox_dir)
    protocols = team_protocol.ProtocolStore()
    team_state = teammates.TeamState()
    mcp_state = mcp.MCPState()
    worktree_state = worktrees.WorktreeState(
        workdir=config.workdir,
        root=config.worktrees_dir,
        run_git=lambda args: _run_git(config, args),
    )
    skills.scan_skills(skill_state)

    hooks = HookRegistry()
    _register_default_hooks(hooks, config, mcp_state)
    registry = ToolRegistry()

    def run_bash(command, run_in_background=False, cwd=None):
        return builtin.run_bash(config.workdir, command, run_in_background, cwd)

    def run_read(path, offset=0, limit=None, cwd=None):
        return builtin.run_read(config.workdir, path, offset, limit, cwd)

    def run_write(path, content, cwd=None):
        return builtin.run_write(config.workdir, path, content, cwd)

    def run_edit(path, old_text, new_text, cwd=None):
        return builtin.run_edit(config.workdir, path, old_text, new_text, cwd)

    def run_glob(pattern, cwd=None):
        return builtin.run_glob(config.workdir, pattern, cwd)
    builtin_handlers = {
        "bash": run_bash,
        "read_file": run_read,
        "write_file": run_write,
        "edit_file": run_edit,
        "glob": run_glob,
        "load_skill": lambda name: skills.load_skill(skill_state, name),
    }
    builtin.register_builtin_tools(registry, builtin_handlers)
    todos.register_todo_tools(registry, session)
    tasks.register_task_tools(registry, task_store)
    scheduler.register_scheduler_tools(registry, scheduler_state, config)

    def scan_unclaimed() -> list[dict]:
        return [
            asdict(task)
            for task in tasks.list_tasks(task_store)
            if task.status == "pending"
            and not task.owner
            and tasks.can_start(task_store, task.id)
        ]

    def wait_for_permission(
        agent: str, request_id: str, deferred_inbox: list[dict]
    ) -> dict:
        return team_protocol.wait_for_permission_response(
            message_bus,
            agent,
            request_id,
            deferred_inbox,
            clock=time.time,
            sleep=time.sleep,
            poll_interval=config.permission_poll_interval,
            timeout=config.permission_timeout,
        )

    def guarded_tool(agent, block, deferred_inbox, handler, cwd):
        request_id = uuid.uuid4().hex
        message_bus.send(
            agent,
            "lead",
            {
                "request_id": request_id,
                "tool_use_id": block.id,
                "tool_name": block.name,
                "tool_input": block.input,
                "cwd": str(cwd) if cwd else None,
            },
            msg_type="permission_request",
        )
        response = wait_for_permission(agent, request_id, deferred_inbox)
        if not response.get("approved"):
            return f"Permission denied: {response.get('reason', 'Permission denied')}", True
        return str(handler(**block.input)), False

    def collect_lead_inbox() -> list[dict]:
        return team_protocol.collect_lead_inbox(
            message_bus,
            protocols,
            hook=hooks.trigger,
            cwd_resolver=lambda cwd: builtin.resolve_tool_cwd(config.workdir, cwd),
            guarded_tools=TEAM_GUARDED_TOOLS,
            clock=time.time,
            sleep=time.sleep,
        )

    def format_inbox(messages: list[dict]) -> str:
        if not messages:
            return "(inbox empty)"
        return "\n".join(
            ["[Team inbox]"]
            + [f"From {item.get('from')}({item.get('type')}){item.get('content', '')}" for item in messages]
        )

    def spawn_teammate(name: str, role: str, prompt: str) -> str:
        _provider, primary, fallback = model_router.snapshot()
        recovery = RecoveryState(primary, fallback)

        def teammate_llm(**kwargs):
            return with_retry(
                lambda: model_router.llm.create(
                    model=recovery.current_model, **kwargs
                ),
                recovery,
                max_transient_retries=config.max_transient_retries,
                max_consecutive_529=config.max_consecutive_529,
                base_delay_ms=config.base_delay_ms,
            )

        teammate_handlers = {
            "bash": run_bash,
            "read_file": run_read,
            "write_file": run_write,
            "send_message": lambda to, content: (
                message_bus.send(name, to, content),
                "Sent",
            )[1],
            "submit_plan": lambda plan: team_protocol.submit_plan(
                message_bus, protocols, name, plan
            ),
            "list_tasks": lambda: tasks._run_list_tasks_tool(task_store),
            "claim_task": lambda task_id: tasks._run_task_operation(
                task_store, "claim", task_id, tasks.claim_task
            ),
            "complete_task": lambda task_id: tasks._run_task_operation(
                task_store, "complete", task_id, tasks.complete_task
            ),
        }

        def idle(
            agent_name,
            messages,
            teammate_name,
            _role,
            worktree_context,
        ):
            return teammates.idle_poll(
                message_bus,
                agent_name,
                messages,
                teammate_name,
                worktree_context,
                scan_unclaimed=scan_unclaimed,
                claim_task=lambda task_id, owner: tasks.claim_task(
                    task_store, task_id, owner
                ),
                worktree_path=lambda name: config.worktrees_dir / name,
                sleep=time.sleep,
                poll_interval=config.idle_poll_interval,
                timeout=config.idle_timeout,
            )

        return teammates.spawn_teammate_thread(
            team_state,
            message_bus,
            teammate_llm,
            name=name,
            role=role,
            prompt=prompt,
            workdir=config.workdir,
            handlers=teammate_handlers,
            hooks=hooks,
            validate_name=team_bus.validate_agent_name,
            guarded_tools=TEAM_GUARDED_TOOLS,
            guarded_tool=guarded_tool,
            idle=idle,
            max_tokens=config.default_max_tokens,
            thread_factory=threading.Thread,
            sleep=time.sleep,
            plan_poll_interval=config.idle_poll_interval,
        )

    team_handlers = {
        "spawn_teammate": spawn_teammate,
        "send_message": lambda to, content: (
            message_bus.send("lead", to, content),
            f"Sent to {to}",
        )[1],
        "check_inbox": lambda: format_inbox(collect_lead_inbox()),
        "request_shutdown": lambda teammate: team_protocol.request_shutdown(
            message_bus, protocols, teammate
        ),
        "request_plan": lambda teammate, task: (
            message_bus.send(
                "lead", teammate, f"Please submit a plan for: {task}", "message"
            ),
            f"Asked {teammate} to submit a plan",
        )[1],
        "review_plan": lambda request_id, approve, feedback="": team_protocol.review_plan(
            message_bus, protocols, request_id, approve, feedback
        ),
    }
    teammates.register_team_tools(registry, team_handlers)
    worktrees.register_worktree_tools(registry, worktree_state, task_store)

    subagent_system = (
        f"You are a coding agent at {config.workdir}. "
        "Complete the task you were given, then return a concise summary. "
        "Do not delegate further."
    )
    subagent_tools = [
        schema
        for schema in builtin.BUILTIN_TOOL_SCHEMAS
        if schema["name"] in {"bash", "read_file", "write_file", "edit_file", "glob"}
    ]

    def spawn_subagent(description: str) -> str:
        _provider, primary, fallback = model_router.snapshot()
        recovery = RecoveryState(primary, fallback)

        def subagent_llm(**kwargs):
            return with_retry(
                lambda: model_router.llm.create(
                    **{**kwargs, "model": recovery.current_model}
                ),
                recovery,
                max_transient_retries=config.max_transient_retries,
                max_consecutive_529=config.max_consecutive_529,
                base_delay_ms=config.base_delay_ms,
            )

        return subagents.spawn_subagent(
            description,
            subagent_llm,
            config,
            subagent_system,
            subagent_tools,
            {name: builtin_handlers[name] for name in ("bash", "read_file", "write_file", "edit_file", "glob")},
            hooks,
            model=primary,
        )

    subagents.register_subagent_tool(registry, spawn_subagent)
    mcp.register_mcp_connection_tool(registry, mcp_state)

    return RuntimeContext(
        config=config,
        llm=llm,
        session=session,
        prompt_builder=PromptBuilder(),
        tools=registry,
        hooks=hooks,
        scheduler=scheduler_state,
        background=background_state,
        tasks=task_store,
        worktrees=worktree_state,
        skills=skill_state,
        memory=memory_store,
        bus=message_bus,
        protocols=protocols,
        team=team_state,
        mcp=mcp_state,
        model=model_router,
    )


def _saved_thinking(record: dict) -> str:
    """Return the validated thinking level stored for one provider."""
    return normalize_level(record.get("thinking")) or DEFAULT_THINKING_LEVEL


def _resolve_active_provider(
    environment: dict[str, str],
    credentials: dict,
) -> tuple:
    """Return (spec, api_key, model_id) for the active non-anthropic provider.

    Returns (None, None, None) when the classic Anthropic/env flow should
    be used instead. Environment keys win over stored credentials so an
    explicitly exported variable keeps overriding stored logins.
    """
    from .adapters.providers import PROVIDERS

    active = auth_store.configured_provider(credentials)
    spec = PROVIDERS.get(active) if isinstance(active, str) else None
    if spec is None or spec.name == "anthropic":
        return None, None, None
    record = auth_store.get_login(credentials, spec.name)
    api_key = environment.get(spec.key_env) or record.get("api_key")
    model_id = record.get("model")
    if not isinstance(model_id, str) or not model_id.strip():
        model_id = spec.default_model
    return spec, api_key, model_id


def load_runtime_environment() -> dict[str, str]:
    from dotenv import dotenv_values

    return _load_environment(
        Path.cwd().resolve(), GLOBAL_SETTINGS_PATH, dict(os.environ), dotenv_values,
    )


def has_local_configuration(credentials_path: Path | None = None) -> bool:
    """Only inspect local records/settings; never probe the remote API."""
    data = auth_store.load_credentials(credentials_path or CREDENTIALS_PATH)
    if auth_store.configured_provider(data):
        return True
    environment = load_runtime_environment()
    return bool(environment.get("MODEL_ID", "").strip() and any(
        environment.get(key, "").strip() for key in (
            "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
        )
    ))


def build_default_runtime(*, credentials_path: Path | None = None) -> RuntimeContext:
    """Load process configuration and construct the production SDK client."""
    from anthropic import Anthropic

    workspace = Path.cwd().resolve()
    environment = load_runtime_environment()
    credentials = auth_store.load_credentials(credentials_path or CREDENTIALS_PATH)
    spec, provider_key, provider_model = _resolve_active_provider(
        environment, credentials
    )
    if spec is not None:
        from .adapters.providers import build_client

        client = build_client(spec, provider_key)
        environment = {**environment, "MODEL_ID": provider_model}
        config = AppConfig.from_env(INSTALL_ROOT, workspace, environment)
        return build_runtime(
            config,
            client,
            progress_factory=terminal_progress,
            provider=spec.name,
            thinking_level=_saved_thinking(
                auth_store.get_login(credentials, spec.name)
            ),
        )

    base_url = environment.get("ANTHROPIC_BASE_URL")
    record = auth_store.get_login(credentials, "anthropic")
    stored_key = record.get("api_key")
    if not isinstance(stored_key, str):
        stored_key = None
    stored_model = record.get("model")
    if stored_key and (
        isinstance(stored_model, str) and stored_model.strip()
        or not environment.get("MODEL_ID", "").strip()
    ):
        from .adapters.providers import get_provider

        environment["MODEL_ID"] = (
            stored_model.strip() if isinstance(stored_model, str) and stored_model.strip()
            else get_provider("anthropic").default_model
        )
    api_key = environment.get("ANTHROPIC_API_KEY") or stored_key
    auth_token = environment.get("ANTHROPIC_AUTH_TOKEN")
    client_options = {}
    if api_key is not None:
        client_options["api_key"] = api_key
    if base_url:
        client_options["base_url"] = base_url
        if api_key is None:
            client_options["api_key"] = ""
    elif api_key is None:
        client_options["api_key"] = ""
    if auth_token is not None:
        client_options["auth_token"] = auth_token
    config = AppConfig.from_env(INSTALL_ROOT, workspace, environment)
    return build_runtime(
        config,
        Anthropic(**client_options),
        progress_factory=terminal_progress,
        thinking_level=_saved_thinking(record),
    )
