from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


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
    primary_model: str
    fallback_model: str | None
    default_max_tokens: int = 8_000
    escalated_max_tokens: int = 64_000
    max_continuations: int = 3
    max_transient_retries: int = 10
    max_reactive_compacts: int = 1
    base_delay_ms: int = 500
    max_consecutive_529: int = 3
    context_limit: int = 50_000
    keep_recent: int = 3
    persist_threshold: int = 20_000
    idle_poll_interval: float = 5.0
    idle_timeout: float = 60.0
    permission_poll_interval: float = 0.5
    permission_timeout: float = 300.0

    @classmethod
    def from_env(
        cls,
        repo_root: Path,
        workdir: Path | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> "AppConfig":
        environment = os.environ if environ is None else environ
        model_id = environment.get("MODEL_ID")
        if model_id is None or not model_id.strip():
            raise MissingConfigurationError("MODEL_ID")
        root = repo_root.resolve()
        workspace = (workdir or repo_root).resolve()
        state_dir = workspace / ".pamu"
        memory_dir = state_dir / "memory"
        tool_result_dir = state_dir / "task_outputs" / "tool-results"
        return cls(
            repo_root=root,
            workdir=workspace,
            state_dir=state_dir,
            skills_dir=workspace / "skills",
            memory_dir=memory_dir,
            memory_index=memory_dir / "MEMORY.md",
            transcripts_dir=state_dir / "transcripts",
            tool_result_dir=tool_result_dir,
            task_dir=state_dir / "tasks",
            mailbox_dir=state_dir / "mailboxes",
            scheduled_tasks_path=state_dir / "scheduled_tasks.json",
            worktrees_dir=state_dir / "worktrees",
            primary_model=model_id,
            fallback_model=environment.get("FALLBACK_MODEL_ID"),
        )
