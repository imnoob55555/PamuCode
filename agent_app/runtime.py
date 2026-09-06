from __future__ import annotations

import threading
from dataclasses import dataclass, field


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
    llm: object | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def snapshot(self) -> tuple[str, str, str | None]:
        """Return (provider, primary_model, fallback_model) atomically."""
        with self.lock:
            return self.provider, self.primary_model, self.fallback_model

    def switch_model(self, model: str) -> None:
        """Keep the provider and adapter, change only the model ids."""
        with self.lock:
            self.primary_model = model

    def switch(
        self,
        provider: str,
        primary_model: str,
        fallback_model: str | None = None,
        llm: object | None = None,
    ) -> None:
        """Switch provider and model together, optionally the adapter."""
        with self.lock:
            self.provider = provider
            self.primary_model = primary_model
            self.fallback_model = fallback_model
            if llm is not None:
                self.llm = llm


@dataclass(slots=True)
class SessionState:
    history: list = field(default_factory=list)
    context: dict = field(default_factory=dict)
    todos: list[dict] = field(default_factory=list)
    rounds_since_todo: int = 0


from .adapters.anthropic import AnthropicAdapter
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
    llm: AnthropicAdapter
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

    def __post_init__(self) -> None:
        # Direct constructions without an explicit router (tests, embedders)
        # still get a consistent, switchable view of the configured model.
        if self.model is None:
            self.model = ModelRouter(
                provider="anthropic",
                primary_model=self.config.primary_model,
                fallback_model=self.config.fallback_model,
                llm=self.llm,
            )
