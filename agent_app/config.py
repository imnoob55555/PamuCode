from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

USER_HOME = Path.home()
SKILL_PARENTS = (".pamu", ".agents", ".claude")


def project_skill_dirs(workspace: Path, home: Path) -> tuple[Path, ...]:
    """Skill directories that come with the workspace and therefore need trust."""
    if Path(workspace).resolve() == Path(home).resolve():
        return ()
    return tuple(workspace / parent / "skills" for parent in SKILL_PARENTS)


def skill_search_dirs(workspace: Path, home: Path) -> tuple[Path, ...]:
    """Project skill directories first, then the user's, without duplicates."""
    candidates = [base / parent / "skills" for base in (workspace, home) for parent in SKILL_PARENTS]
    return tuple(dict.fromkeys(candidates))


def _positive_int(environment: Mapping[str, str], name: str, default: int) -> int:
    raw = environment.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


class MissingConfigurationError(ValueError):
    """A required PamuCode configuration value was not provided."""

    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(f"Missing required configuration: {key}")


@dataclass(frozen=True, slots=True)
class AppConfig:
    repo_root: Path
    workdir: Path
    state_dir: Path
    skills_dir: Path
    memory_dir: Path
    memory_index: Path
    transcripts_dir: Path
    tool_result_dir: Path
    task_dir: Path
    mailbox_dir: Path
    scheduled_tasks_path: Path
    worktrees_dir: Path
    logs_dir: Path
    primary_model: str
    fallback_model: str | None
    context_window: int | None = None
    context_window_target: tuple[str, str, str] | None = None
    log_level: str = "INFO"
    default_max_tokens: int = 8_000
    escalated_max_tokens: int = 64_000
    max_continuations: int = 3
    max_transient_retries: int = 10
    max_reactive_compacts: int = 1
    base_delay_ms: int = 500
    max_consecutive_529: int = 3
    persist_threshold: int = 20_000
    idle_poll_interval: float = 5.0
    idle_timeout: float = 60.0
    permission_poll_interval: float = 0.5
    permission_timeout: float = 300.0
    headless: bool = False
    sandbox_mode: str = "off"
    skill_dirs: tuple[Path, ...] = ()
    project_skill_dirs: tuple[Path, ...] = ()
    trust_project_skills: bool = False
    shell_approval: str = "ask"
    max_turn_model_calls: int = 50
    max_turn_tool_calls: int = 200
    max_turn_tokens: int = 2_000_000
    max_teammates: int = 4
    max_background_tasks: int = 8
    auto_memory: bool = True

    @property
    def sessions_dir(self) -> Path:
        return self.state_dir / "sessions"

    @property
    def checkpoints_dir(self) -> Path:
        return self.state_dir / "checkpoints"

    @classmethod
    def from_env(
        cls,
        repo_root: Path,
        workdir: Path | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> AppConfig:
        environment = os.environ if environ is None else environ
        model_id = environment.get("MODEL_ID")
        if model_id is None or not model_id.strip():
            raise MissingConfigurationError("MODEL_ID")
        from .core.context_budget import validate_window
        raw_window = environment.get("PAMU_CONTEXT_WINDOW", "").strip()
        window = validate_window(raw_window) if raw_window else None
        sandbox = environment.get("PAMU_SANDBOX", "off").strip()
        if sandbox not in {"off", "confined"}:
            raise ValueError("PAMU_SANDBOX must be 'off' or 'confined'")
        limits = {
            field: _positive_int(environment, name, default)
            for field, name, default in (
                ("max_turn_model_calls", "PAMU_MAX_TURN_MODEL_CALLS", 50),
                ("max_turn_tool_calls", "PAMU_MAX_TURN_TOOL_CALLS", 200),
                ("max_turn_tokens", "PAMU_MAX_TURN_TOKENS", 2_000_000),
                ("max_teammates", "PAMU_MAX_TEAMMATES", 4),
                ("max_background_tasks", "PAMU_MAX_BACKGROUND_TASKS", 8),
            )
        }
        auto_memory = environment.get("PAMU_AUTO_MEMORY", "on").strip().lower() or "on"
        if auto_memory not in {"on", "off"}:
            raise ValueError("PAMU_AUTO_MEMORY must be 'on' or 'off'")
        shell_approval = environment.get("PAMU_SHELL_APPROVAL", "ask").strip() or "ask"
        if shell_approval not in {"ask", "auto"}:
            raise ValueError("PAMU_SHELL_APPROVAL must be 'ask' or 'auto'")
        root = repo_root.resolve()
        workspace = (workdir or repo_root).resolve()
        state_dir = workspace / ".pamu"
        memory_dir = state_dir / "memory"
        tool_result_dir = state_dir / "task_outputs" / "tool-results"
        return cls(
            repo_root=root,
            workdir=workspace,
            state_dir=state_dir,
            skills_dir=state_dir / "skills",
            skill_dirs=skill_search_dirs(workspace, USER_HOME),
            project_skill_dirs=project_skill_dirs(workspace, USER_HOME),
            trust_project_skills=environment.get("PAMU_TRUST_PROJECT_SKILLS", "").strip().lower()
            in {"1", "true", "yes", "on"},
            memory_dir=memory_dir,
            memory_index=memory_dir / "MEMORY.md",
            transcripts_dir=state_dir / "transcripts",
            tool_result_dir=tool_result_dir,
            task_dir=state_dir / "tasks",
            mailbox_dir=state_dir / "mailboxes",
            scheduled_tasks_path=state_dir / "scheduled_tasks.json",
            worktrees_dir=state_dir / "worktrees",
            logs_dir=state_dir / "logs",
            primary_model=model_id,
            context_window=window,
            fallback_model=environment.get("FALLBACK_MODEL_ID"),
            log_level=(environment.get("LOG_LEVEL") or "INFO").strip() or "INFO",
            sandbox_mode=sandbox,
            shell_approval=shell_approval,
            auto_memory=auto_memory == "on",
            **limits,
        )
