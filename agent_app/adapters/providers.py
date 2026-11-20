"""Explicit provider protocols, client construction, and runtime switching."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..runtime import RuntimeContext
from .anthropic import AnthropicAdapter, DEEPSEEK_MESSAGES_URL


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    name: str
    base_url: str
    key_env: str = ""
    default_model: str = ""
    label: str = ""
    requires_key: bool = True
    protocol: str = "anthropic_messages"
    base_url_env: str = ""


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
        DEEPSEEK_MESSAGES_URL,
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
    "openai": ProviderSpec(
        "openai", "https://api.openai.com/v1", "OPENAI_API_KEY", "",
        "OpenAI 官方", True, "openai_responses", "OPENAI_BASE_URL",
    ),
    "openai-compatible": ProviderSpec(
        "openai-compatible", "", "OPENAI_COMPATIBLE_API_KEY", "",
        "OpenAI 兼容服务", False, "openai_chat", "OPENAI_COMPATIBLE_BASE_URL",
    ),
    "ollama": ProviderSpec(
        "ollama", "http://localhost:11434", "", "",
        "Ollama 本地模型", False, "ollama", "OLLAMA_BASE_URL",
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
    openai_factory: Callable[..., Any] | None = None,
    httpx_factory: Callable[..., Any] | None = None,
) -> Any:
    """Construct the selected SDK/HTTP client, without making a request."""
    custom_anthropic = spec.name == "anthropic" and base_url and base_url != spec.base_url
    if spec.requires_key and not (api_key or auth_token or custom_anthropic):
        raise MissingApiKeyError(spec)
    options: dict[str, Any] = {"base_url": base_url or spec.base_url}
    if spec.protocol in ("openai_responses", "openai_chat"):
        from openai import OpenAI

        # Satisfy SDK constructor validation while sending no auth header for
        # keyless services. Never let the SDK discover another provider's key.
        return (openai_factory or OpenAI)(**options, api_key=api_key or "pamu-keyless", max_retries=0)
    if spec.protocol == "ollama":
        import httpx

        return (httpx_factory or httpx.Client)(**options, timeout=600.0)
    from anthropic import Anthropic

    factory = anthropic_factory or Anthropic
    if spec.name == "anthropic" and not base_url and spec.base_url == PROVIDERS["anthropic"].base_url:
        options.pop("base_url")
    if spec.name == "openrouter":
        options.update(api_key="", auth_token=api_key)
    elif api_key:
        options["api_key"] = api_key
    if auth_token is not None:
        options["auth_token"] = auth_token
        options.setdefault("api_key", "")
    options.setdefault("api_key", "")
    client = factory(**options)
    if hasattr(client, "max_retries"):
        client.max_retries = 0
    return client


def build_adapter(spec, client, *, progress=None, base_url=None, keyless=False):
    from .common import null_progress
    from .openai_responses import OpenAIResponsesAdapter
    from .openai_chat import OpenAIChatAdapter
    from .ollama import OllamaAdapter

    adapters = {
        "anthropic_messages": AnthropicAdapter,
        "openai_responses": OpenAIResponsesAdapter,
        "openai_chat": OpenAIChatAdapter,
        "ollama": OllamaAdapter,
    }
    origin = f"{spec.name}@{(base_url or spec.base_url).rstrip('/')}"
    options = {"omit_auth": keyless} if spec.protocol in ("openai_responses", "openai_chat") else {}
    return adapters[spec.protocol](client, progress or null_progress, origin=origin, **options)


def verify_login(
    spec: ProviderSpec,
    api_key: str | None,
    *,
    anthropic_factory: Callable[..., Any] | None = None,
    model: str | None = None,
) -> bool:
    """Validate the selected protocol/model with a bounded live request."""
    client = None
    try:
        client = build_client(spec, api_key, anthropic_factory=anthropic_factory)
        kwargs = dict(model=model or spec.default_model,
                      messages=[{"role": "user", "content": "ping"}],
                      max_tokens=1 if spec.protocol == "anthropic_messages" else 256)
        if spec.protocol == "anthropic_messages":
            client.messages.create(**kwargs)
        else:
            build_adapter(spec, client, keyless=not api_key).create(system=None, tools=None, **kwargs)
    except Exception:
        return False
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()
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
    thinking: str | None = None,
    openai_factory: Callable[..., Any] | None = None,
    httpx_factory: Callable[..., Any] | None = None,
    model_source: str = "运行时配置",
) -> Any:
    """Switch a live runtime to ``spec`` by swapping adapter and router.

    ``thinking`` restores the provider's saved thinking level; ``None`` or
    an unknown value falls back to ``off``.
    """
    client = build_client(
        spec,
        api_key,
        anthropic_factory=anthropic_factory,
        base_url=base_url,
        auth_token=auth_token,
        openai_factory=openai_factory,
        httpx_factory=httpx_factory,
    )
    adapter = build_adapter(spec, client, progress=progress, base_url=base_url, keyless=not api_key)
    router = runtime.model
    if router is None:
        raise RuntimeError("runtime is missing its model router")
    router.switch(
        spec.name,
        model or spec.default_model,
        None,
        adapter,
        thinking_level=(thinking or "off") if spec.protocol == "anthropic_messages" else "off",
        protocol=spec.protocol,
        base_url=base_url or spec.base_url,
        model_source=model_source,
    )
    runtime.llm = adapter
    return adapter
