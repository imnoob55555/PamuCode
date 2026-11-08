"""Credential storage for model providers.

Credentials live in ``~/.pamu/credentials.json`` with owner-only
permissions. The file shape is::

    {
      "providers": {"deepseek": {"api_key": "...", "model": "deepseek-chat"}},
      "active": "deepseek"
    }

``active`` selects the provider used on the next launch; the
Anthropic/env-variable flow remains the default when it is ``null``.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

CREDENTIALS_PATH = Path.home() / ".pamu" / "credentials.json"


def _empty() -> dict[str, Any]:
    return {"providers": {}, "active": None}


def load_credentials(path: Path = CREDENTIALS_PATH) -> dict[str, Any]:
    """Read the credential store, degrading to empty on any defect."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _empty()
    if not isinstance(raw, dict):
        return _empty()
    providers = raw.get("providers")
    active = raw.get("active")
    return {
        "providers": providers if isinstance(providers, dict) else {},
        "active": active if isinstance(active, str) else None,
    }


def save_credentials(data: dict[str, Any], path: Path = CREDENTIALS_PATH) -> None:
    """Atomically replace the store; the key is owner-only from creation."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def get_login(data: dict[str, Any], provider: str) -> dict[str, Any]:
    """Return the stored record for ``provider`` (empty dict if absent)."""
    record = data.get("providers", {}).get(provider)
    return record if isinstance(record, dict) else {}


def configured_provider(data: dict[str, Any]) -> str | None:
    """Choose a known locally saved account without contacting any API."""
    from ..adapters.providers import PROVIDERS

    active = data.get("active")
    names = ([active] if isinstance(active, str) else []) + list(PROVIDERS)
    for name in names:
        if name not in PROVIDERS:
            continue
        record = get_login(data, name)
        key = record.get("api_key")
        if PROVIDERS[name].requires_key:
            if isinstance(key, str) and key.strip():
                return name
        elif isinstance(record.get("model"), str) and record["model"].strip():
            if name != "openai-compatible" or record.get("base_url"):
                return name
    return None


def save_key(provider: str, api_key: str, path: Path = CREDENTIALS_PATH) -> None:
    """Save the first onboarding step without changing the active account."""
    data = load_credentials(path)
    record = get_login(data, provider)
    record["api_key"] = api_key
    data["providers"][provider] = record
    save_credentials(data, path)


def store_login(
    provider: str,
    api_key: str,
    model: str | None = None,
    path: Path = CREDENTIALS_PATH,
    *,
    base_url: str | None = None,
    protocol: str | None = None,
) -> dict[str, Any]:
    """Store a verified credential and make it the active provider."""
    data = load_credentials(path)
    record = get_login(data, provider)
    record["api_key"] = api_key
    if model:
        record["model"] = model
    if base_url is not None:
        record["base_url"] = base_url
    if protocol is not None:
        record["protocol"] = protocol
    data["providers"][provider] = record
    data["active"] = provider
    save_credentials(data, path)
    return data


def remove_login(provider: str, path: Path = CREDENTIALS_PATH) -> dict[str, Any]:
    """Forget one provider's credential, clearing active if it pointed there."""
    data = load_credentials(path)
    data.get("providers", {}).pop(provider, None)
    if data.get("active") == provider:
        data["active"] = None
    save_credentials(data, path)
    return data


def set_active(provider: str, path: Path = CREDENTIALS_PATH) -> dict[str, Any]:
    """Persist which provider the next launch should use."""
    data = load_credentials(path)
    data["active"] = provider
    save_credentials(data, path)
    return data


def set_model(
    provider: str, model: str, path: Path = CREDENTIALS_PATH
) -> dict[str, Any]:
    """Persist the default model of a logged-in provider."""
    data = load_credentials(path)
    record = get_login(data, provider)
    if not record:
        return data
    record["model"] = model
    data["providers"][provider] = record
    save_credentials(data, path)
    return data


def set_thinking(
    provider: str, level: str, path: Path = CREDENTIALS_PATH
) -> dict[str, Any]:
    """Persist the thinking level of a logged-in provider."""
    data = load_credentials(path)
    record = get_login(data, provider)
    if not record:
        return data
    record["thinking"] = level
    data["providers"][provider] = record
    save_credentials(data, path)
    return data
