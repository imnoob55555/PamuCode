"""Model provider registry, client construction, and runtime switching.

Every provider below exposes an Anthropic-compatible Messages API, so
they all reuse :class:`agent_app.adapters.anthropic.AnthropicAdapter`.
Adding a provider means adding one :class:`ProviderSpec` entry.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from ..runtime import RuntimeContext
from .anthropic import AnthropicAdapter


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    name: str
    base_url: str
    key_env: str = ""
    default_model: str = ""
    label: str = ""
    requires_key: bool = True


PROVIDERS: dict[str, ProviderSpec] = {
    "anthropic": ProviderSpec(
        "anthropic",
        "https://api.anthropic.com",
        "ANTHROPIC_API_KEY",
        "claude-sonnet-4-6",
        "Anthropic 官方",
    ),
    "deepseek": ProviderSpec(
        "deepseek",
        "https://api.deepseek.com/anthropic",
        "DEEPSEEK_API_KEY",
        "deepseek-chat",
        "DeepSeek",
    ),
    "moonshot": ProviderSpec(
        "moonshot",
        "https://api.moonshot.cn/anthropic",
        "MOONSHOT_API_KEY",
        "kimi-k2-0905-preview",
        "Moonshot Kimi",
    ),
    "zhipu": ProviderSpec(
        "zhipu",
        "https://open.bigmodel.cn/api/anthropic",
        "ZHIPU_API_KEY",
        "glm-4.6",
        "智谱 GLM",
    ),
    "openrouter": ProviderSpec(
        "openrouter",
        "https://openrouter.ai/api",
        "OPENROUTER_API_KEY",
        "z-ai/glm-4.6",
        "OpenRouter",
    ),
}

# Offline presets; account access is only checked on the user's first request.
# Keep saved/custom IDs selectable when the provider's catalog changes.
MODEL_PRESETS: dict[str, tuple[str, ...]] = {
    "anthropic": ("claude-sonnet-4-6", "claude-opus-4-7", "claude-haiku-4-5"),
    "deepseek": ("deepseek-v4-flash", "deepseek-v4-pro"),
    "moonshot": ("kimi-k3", "kimi-k2.5"),
    "zhipu": ("glm-4.7", "glm-5.2"),
    "openrouter": ("anthropic/claude-sonnet-4.6", "anthropic/claude-haiku-4.5"),
}


def model_choices(spec: ProviderSpec, current: str | None = None) -> tuple[str, ...]:
    candidates = (current, *MODEL_PRESETS.get(spec.name, ()), spec.default_model)
    return tuple(
        dict.fromkeys(
            value.strip()
            for value in candidates
            if isinstance(value, str) and value.strip()
        )
    )


class UnknownProviderError(KeyError):
    """A provider name that is not in the registry."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"Unknown provider: {name}")


class MissingApiKeyError(ValueError):
    """A key-requiring provider was built without a credential."""

    def __init__(self, spec: ProviderSpec) -> None:
        self.spec = spec
        super().__init__(
            f"Provider '{spec.name}' requires an API key "
            f"(set {spec.key_env} or run: pamu login {spec.name})"
        )


def get_provider(name: str) -> ProviderSpec:
    """Return the spec for ``name`` or raise UnknownProviderError."""
    try:
        return PROVIDERS[name]
    except KeyError:
        raise UnknownProviderError(name) from None


def build_client(
    spec: ProviderSpec,
    api_key: str | None,
    *,
    anthropic_factory: Callable[..., Any] | None = None,
    base_url: str | None = None,
    auth_token: str | None = None,
) -> Any:
    """Construct an Anthropic SDK client pointed at the provider."""
    from anthropic import Anthropic

    factory = anthropic_factory or Anthropic
    if spec.requires_key and not (api_key or auth_token):
        raise MissingApiKeyError(spec)
    options: dict[str, Any] = {"base_url": base_url or spec.base_url}
    if spec.name == "openrouter":
        options.update(api_key="", auth_token=api_key)
    elif api_key:
        options["api_key"] = api_key
    if auth_token is not None:
        options["auth_token"] = auth_token
        options.setdefault("api_key", "")
    return factory(**options)


def verify_login(
    spec: ProviderSpec,
    api_key: str | None,
    *,
    anthropic_factory: Callable[..., Any] | None = None,
    model: str | None = None,
) -> bool:
    """Validate credentials with a minimal one-token live request."""
    try:
        client = build_client(spec, api_key, anthropic_factory=anthropic_factory)
        client.messages.create(
            model=model or spec.default_model,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=1,
        )
    except Exception:
        return False
    return True


def apply_provider(
    runtime: RuntimeContext,
    spec: ProviderSpec,
    api_key: str | None,
    model: str | None = None,
    *,
    anthropic_factory: Callable[..., Any] | None = None,
    progress: Callable[[str], Any] | None = None,
    base_url: str | None = None,
    auth_token: str | None = None,
) -> AnthropicAdapter:
    """Switch a live runtime to ``spec`` by swapping adapter and router."""
    client = build_client(
        spec,
        api_key,
        anthropic_factory=anthropic_factory,
        base_url=base_url,
        auth_token=auth_token,
    )
    adapter = (
        AnthropicAdapter(client, progress) if progress else AnthropicAdapter(client)
    )
    runtime.model.switch(
        spec.name,
        model or spec.default_model,
        None,
        adapter,
    )
    runtime.llm = adapter
    return adapter
