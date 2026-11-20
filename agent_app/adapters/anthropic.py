"""Anthropic SDK request and streaming boundary."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any

from .common import anthropic_history, null_progress, scope_anthropic, visible_stream

DEEPSEEK_MESSAGES_URL = 'https://api.deepseek.com/anthropic'


def is_deepseek_messages_endpoint(origin: str) -> bool:
    provider, separator, url = origin.partition('@')
    # The adapter's replay origin can carry a provider prefix. Match the
    # endpoint itself because users can reach it through the Anthropic preset.
    endpoint = url if separator and '://' not in provider else origin
    return endpoint.rstrip('/') == DEEPSEEK_MESSAGES_URL


@dataclass(frozen=True, slots=True)
class AnthropicAdapter:
    client: Any
    progress: Callable[[str], AbstractContextManager] = null_progress
    origin: str = ""

    def project_request(self, *, system, messages, tools, model):
        return {'system': system, 'messages': anthropic_history(messages, origin=self.origin, model=model), 'tools': tools}

    def _thinking(self, thinking):
        if thinking is None and is_deepseek_messages_endpoint(self.origin):
            return {'type': 'disabled'}
        return thinking

    def create(
        self,
        *,
        system,
        messages,
        model,
        max_tokens,
        tools,
        thinking=None,
    ):
        request = {
            "model": model,
            "messages": anthropic_history(messages, origin=self.origin, model=model),
            "max_tokens": max_tokens,
        }
        if system is not None:
            request["system"] = system
        if tools is not None:
            request["tools"] = tools
        thinking = self._thinking(thinking)
        if thinking is not None:
            request["thinking"] = thinking
        return scope_anthropic(self.client.messages.create(**request), self.origin, model)

    def create_streaming(
        self,
        *,
        system,
        messages,
        model,
        max_tokens,
        tools,
        thinking=None,
    ):
        request = {
            "model": model,
            "system": system,
            "messages": anthropic_history(messages, origin=self.origin, model=model),
            "tools": tools,
            "max_tokens": max_tokens,
        }
        thinking = self._thinking(thinking)
        if thinking is not None:
            request["thinking"] = thinking
        with visible_stream(self.progress) as emit, self.client.messages.stream(**request) as stream:
            for chunk in stream.text_stream:
                if chunk:
                    emit(chunk)
            return scope_anthropic(stream.get_final_message(), self.origin, model)
