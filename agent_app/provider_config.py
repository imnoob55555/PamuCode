"""Resolve one provider consistently for startup, setup and live switching."""

from dataclasses import dataclass, field, replace
from urllib.parse import urlsplit, urlunsplit

from .adapters.providers import ProviderSpec, get_provider, UnknownProviderError, MissingApiKeyError
from .config import MissingConfigurationError
from .features import auth


class ProviderConfigurationError(ValueError):
    pass


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def normalize_base_url(value: str) -> str:
    try:
        parts = urlsplit(value.strip())
        valid = (parts.scheme in ("http", "https") and parts.hostname and
                 not parts.username and not parts.password and
                 not parts.query and not parts.fragment and
                 not any(c.isspace() for c in value))
        parts.port  # Validate malformed/out-of-range ports without printing the URL.
    except ValueError:
        valid = False
    if not valid:
        raise ProviderConfigurationError("服务地址须为 HTTP(S) URL，不能包含凭证、查询参数或片段。")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip('/'), '', ''))


@dataclass(frozen=True, slots=True)
class ResolvedProviderConfig:
    spec: ProviderSpec
    protocol: str
    base_url: str
    model: str
    model_source: str
    api_key: str | None = field(default=None, repr=False)
    auth_token: str | None = field(default=None, repr=False)


def resolve_provider_config(environment, credentials, provider=None):
    name = (provider or _text(environment.get("PAMU_PROVIDER")) or
            credentials.get("active") or auth.configured_provider(credentials) or "anthropic")
    try:
        spec = get_provider(name)
    except (UnknownProviderError, TypeError):
        raise ProviderConfigurationError("未知供应商，请用 pamu login 选择受支持的供应商。") from None
    record = auth.get_login(credentials, name)
    protocol = spec.protocol
    if name == "openai-compatible":
        protocol = (_text(environment.get("OPENAI_COMPATIBLE_PROTOCOL")) or
                    _text(record.get("protocol")) or protocol)
        if protocol not in ("openai_responses", "openai_chat"):
            raise ProviderConfigurationError("OPENAI_COMPATIBLE_PROTOCOL 须为 openai_responses 或 openai_chat。")
    url_env = spec.base_url_env or name.upper() + "_BASE_URL"
    base_url = _text(environment.get(url_env)) or _text(record.get("base_url")) or spec.base_url
    if not base_url:
        raise ProviderConfigurationError(f"请配置 {url_env} 或运行 pamu login {name} 设置服务地址。")
    base_url = normalize_base_url(base_url)
    model = _text(environment.get("MODEL_ID"))
    source = "环境配置"
    if not model:
        model, source = _text(record.get("model")), "已保存配置"
    if not model and (record or provider or environment.get("PAMU_PROVIDER")):
        model, source = spec.default_model, "内置默认"
    if not model:
        raise MissingConfigurationError("MODEL_ID")
    key = _text(environment.get(spec.key_env)) or _text(record.get("api_key")) or None
    token = (_text(environment.get("ANTHROPIC_AUTH_TOKEN")) or None) if name == "anthropic" else None
    if name == "ollama":
        key = None
    custom_anthropic = name == "anthropic" and base_url != spec.base_url
    if spec.requires_key and not (key or token or custom_anthropic):
        raise MissingApiKeyError(spec)
    # Carry the selected wire protocol/address to every client construction path.
    return ResolvedProviderConfig(replace(spec, protocol=protocol), protocol, base_url,
                                  model, source, key, token)
