# ruff: noqa: E402
# The imports at the bottom of this module are late on purpose: features
# modules import RuntimeContext/SessionState from here, so hoisting them
# would create an import cycle.
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from .thinking import DEFAULT_THINKING_LEVEL, normalize_level


@dataclass(slots=True)
class ModelRouter:
    """Mutable selection of the active provider and model.

    ``AppConfig`` stays frozen; runtime switching (``/model`` and
    ``/provider`` REPL commands) mutates this router instead. The
    ``llm`` slot holds the adapter that belongs to ``provider`` so a
    provider switch swaps the client together with the model ids.
    """

    provider: str = "anthropic"
    primary_model: str = ""
    fallback_model: str | None = None
    llm: Any = None
    thinking_level: str = "off"
    lock: threading.Lock = field(default_factory=threading.Lock)
    protocol: str = "anthropic_messages"
    base_url: str = ""
    model_source: str = "运行时配置"

    def snapshot(self) -> tuple[str, str, str | None]:
        """Return (provider, primary_model, fallback_model) atomically."""
        with self.lock:
            return self.provider, self.primary_model, self.fallback_model

    def thinking_snapshot(self) -> str:
        """Return the active thinking level atomically."""
        with self.lock:
            return self.thinking_level

    def switch_thinking(self, level: str) -> None:
        """Change only the thinking level for the current provider."""
        with self.lock:
            self.thinking_level = normalize_level(level) or DEFAULT_THINKING_LEVEL

    def switch_model(self, model: str) -> None:
        """Keep the provider and adapter, change only the model ids."""
        with self.lock:
            self.primary_model = model
            self.model_source = "当前会话选择"

    def switch(
        self,
        provider: str,
        primary_model: str,
        fallback_model: str | None = None,
        llm: object | None = None,
        thinking_level: str | None = None,
        *,
        protocol: str | None = None,
        base_url: str | None = None,
        model_source: str | None = None,
    ) -> None:
        """Switch provider and model together, optionally the adapter."""
        with self.lock:
            self.provider = provider
            self.primary_model = primary_model
            self.fallback_model = fallback_model
            if llm is not None:
                self.llm = llm
            if protocol is not None:
                self.protocol = protocol
            if base_url is not None:
                self.base_url = base_url
            if model_source is not None:
                self.model_source = model_source
            if thinking_level is not None:
                self.thinking_level = (
                    normalize_level(thinking_level) or DEFAULT_THINKING_LEVEL
                )


@dataclass(slots=True)
class SessionState:
    history: list = field(default_factory=list)
    context: dict = field(default_factory=dict)
    todos: list[dict] = field(default_factory=list)
    rounds_since_todo: int = 0
    context_state: dict = field(default_factory=dict)
    context_archive: Any = field(default=None, repr=False, compare=False)


from .config import AppConfig
from .core.prompt import PromptBuilder
from .features.background import BackgroundState
from .features.mcp import MCPState
from .features.memory import MemoryStore
from .features.scheduler import SchedulerState
from .features.skills import SkillState
from .features.tasks import TaskStore
from .features.teams.bus import MessageBus
from .features.teams.protocol import ProtocolStore
from .features.teams.teammates import TeamState
from .features.worktrees import WorktreeState
from .tools.hooks import HookRegistry
from .tools.registry import ToolRegistry


@dataclass(slots=True)
class RuntimeContext:
    config: AppConfig
    llm: Any
    session: SessionState
    prompt_builder: PromptBuilder
    tools: ToolRegistry
    hooks: HookRegistry
    scheduler: SchedulerState
    background: BackgroundState
    tasks: TaskStore
    worktrees: WorktreeState
    skills: SkillState
    memory: MemoryStore
    bus: MessageBus
    protocols: ProtocolStore
    team: TeamState
    mcp: MCPState
    model: ModelRouter | None = None
    agent_lock: threading.Lock = field(default_factory=threading.Lock)
    stop_event: threading.Event = field(default_factory=threading.Event)

    sessions: Any = None
    checkpoints: Any = None

    def __post_init__(self) -> None:
        # Direct constructions without an explicit router (tests, embedders)
        # still get a consistent, switchable view of the configured model.
        from .working_mode import tool_denial
        self.tools.access_check = lambda name: tool_denial(self.session, name)
        if self.model is None:
            self.model = ModelRouter(
                provider="anthropic",
                primary_model=self.config.primary_model,
                fallback_model=self.config.fallback_model,
                llm=self.llm,
            )
