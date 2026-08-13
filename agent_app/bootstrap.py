"""Application composition root with no import-time runtime side effects."""

from __future__ import annotations

import os
import stat
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from .adapters.anthropic import AnthropicAdapter
from .cli_progress import terminal_progress
from .config import AppConfig
from .core.compaction import persist_large_output
from .core.prompt import PromptBuilder
from .core.recovery import RecoveryState, with_retry
from .features import background, mcp, memory, scheduler, skills
from .features import subagents, tasks, todos, worktrees
from .features.teams import bus as team_bus
from .features.teams import protocol as team_protocol
from .features.teams import teammates
from .runtime import RuntimeContext, SessionState
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
GLOBAL_ENV_PATH = Path.home() / ".config" / "pamucode" / ".env"
TEAM_GUARDED_TOOLS = {"bash", "write_file"}


def _load_environment(
    workspace: Path,
    global_env: Path,
    process_environment: Mapping[str, str],
    read: Callable[[Path], Mapping[str, str | None]],
) -> dict[str, str]:
    """Build isolated workspace settings without mutating the process."""

    def defined(values: Mapping[str, str | None]) -> dict[str, str]:
        return {name: value for name, value in values.items() if value is not None}

    project_values = defined(read(workspace / ".pamu" / ".env"))
    global_values = defined(read(global_env))
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

        resolved = candidate.resolve(strict=False)
        if resolved != expected:
            raise ValueError(
                f"PamuCode state path resolves outside the state root: {candidate}"
            )


_DIRECTORY_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)


@contextmanager
def _closing_fd(descriptor: int):
    try:
        yield descriptor
    finally:
        os.close(descriptor)


def _open_or_create_directory(parent_fd: int, name: str) -> int:
    try:
        os.mkdir(name, dir_fd=parent_fd)
    except FileExistsError:
        pass
    return os.open(name, _DIRECTORY_OPEN_FLAGS, dir_fd=parent_fd)


def _verify_named_directory(
    parent_fd: int, name: str, directory_fd: int, label: str
) -> None:
    named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    opened = os.fstat(directory_fd)
    if not stat.S_ISDIR(named.st_mode) or (
        named.st_dev,
        named.st_ino,
    ) != (opened.st_dev, opened.st_ino):
        raise OSError(f"PamuCode {label} changed during initialization")


def _create_directory_path(state_fd: int, parts: tuple[str, ...]) -> None:
    current_fd = os.dup(state_fd)
    try:
        for part in parts:
            child_fd = _open_or_create_directory(current_fd, part)
            os.close(current_fd)
            current_fd = child_fd
    finally:
        os.close(current_fd)


def _create_state_gitignore(state_fd: int, ignore: Path) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(".gitignore", flags, 0o666, dir_fd=state_fd)
    except FileExistsError:
        mode = os.stat(
            ".gitignore", dir_fd=state_fd, follow_symlinks=False
        ).st_mode
        if not stat.S_ISREG(mode):
            raise ValueError(
                f"PamuCode state .gitignore must be a regular file: {ignore}"
            )
    else:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write("*\n!.gitignore\n")


def _create_storage_roots(config: AppConfig) -> None:
    _validate_state_paths(config)
    config.workdir.mkdir(parents=True, exist_ok=True)
    with _closing_fd(
        os.open(config.workdir.parent, _DIRECTORY_OPEN_FLAGS)
    ) as parent_fd:
        workspace_name = config.workdir.name or "."
        with _closing_fd(
            os.open(
                workspace_name,
                _DIRECTORY_OPEN_FLAGS,
                dir_fd=parent_fd,
            )
        ) as workspace_fd:
            _verify_named_directory(
                parent_fd, workspace_name, workspace_fd, "workspace"
            )
            with _closing_fd(
                _open_or_create_directory(workspace_fd, ".pamu")
            ) as state_fd:
                _validate_state_paths(config)
                _verify_named_directory(
                    parent_fd, workspace_name, workspace_fd, "workspace"
                )
                _create_state_gitignore(
                    state_fd, config.state_dir / ".gitignore"
                )
                for root in (
                    config.memory_dir,
                    config.transcripts_dir,
                    config.tool_result_dir,
                    config.task_dir,
                    config.mailbox_dir,
                    config.worktrees_dir,
                    config.scheduled_tasks_path.parent,
                ):
                    relative = root.relative_to(config.state_dir)
                    _create_directory_path(state_fd, relative.parts)
                _validate_state_paths(config)
                _verify_named_directory(
                    parent_fd, workspace_name, workspace_fd, "workspace"
                )
                _verify_named_directory(
                    workspace_fd, ".pamu", state_fd, "state root"
                )


def _run_git(config: AppConfig, args: list[str]) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["git", *args],
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
    config: AppConfig, sdk_client, *, progress_factory=None
) -> RuntimeContext:
    """Build one completely independent runtime from explicit dependencies."""
    _create_storage_roots(config)
    llm = (
        AnthropicAdapter(sdk_client, progress_factory)
        if progress_factory is not None
        else AnthropicAdapter(sdk_client)
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

    run_bash = lambda command, run_in_background=False, cwd=None: builtin.run_bash(
        config.workdir, command, run_in_background, cwd
    )
    run_read = lambda path, offset=0, limit=None, cwd=None: builtin.run_read(
        config.workdir, path, offset, limit, cwd
    )
    run_write = lambda path, content, cwd=None: builtin.run_write(
        config.workdir, path, content, cwd
    )
    run_edit = lambda path, old_text, new_text, cwd=None: builtin.run_edit(
        config.workdir, path, old_text, new_text, cwd
    )
    run_glob = lambda pattern, cwd=None: builtin.run_glob(
        config.workdir, pattern, cwd
    )
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
        recovery = RecoveryState(config.primary_model, config.fallback_model)

        def teammate_llm(**kwargs):
            return with_retry(
                lambda: llm.create(model=recovery.current_model, **kwargs),
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
        recovery = RecoveryState(config.primary_model, config.fallback_model)

        def subagent_llm(**kwargs):
            return with_retry(
                lambda: llm.create(
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
    )


def build_default_runtime() -> RuntimeContext:
    """Load process configuration and construct the production SDK client."""
    from anthropic import Anthropic
    from dotenv import dotenv_values

    workspace = Path.cwd().resolve()
    environment = _load_environment(
        workspace,
        GLOBAL_ENV_PATH,
        dict(os.environ),
        dotenv_values,
    )
    base_url = environment.get("ANTHROPIC_BASE_URL")
    api_key = environment.get("ANTHROPIC_API_KEY")
    auth_token = environment.get("ANTHROPIC_AUTH_TOKEN")
    client_options = {}
    if api_key is not None:
        client_options["api_key"] = api_key
    if base_url:
        client_options["base_url"] = base_url
        if api_key is None:
            client_options["api_key"] = ""
    elif auth_token is not None:
        client_options["auth_token"] = auth_token
    elif api_key is None:
        client_options["api_key"] = ""
    config = AppConfig.from_env(INSTALL_ROOT, workspace, environment)
    return build_runtime(
        config,
        Anthropic(**client_options),
        progress_factory=terminal_progress,
    )
