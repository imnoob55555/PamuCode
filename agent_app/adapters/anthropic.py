"""Anthropic SDK request and streaming boundary."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any

from ..core.recovery import PartialStreamError
from .common import anthropic_history, scope_anthropic


class _NullProgress:
    def __enter__(self):
        return self

    def stop(self) -> None:
        pass

    def __exit__(self, exc_type, exc, traceback):
        return False


def _null_progress(_label: str) -> AbstractContextManager:
    return _NullProgress()


@dataclass(frozen=True, slots=True)
class AnthropicAdapter:
    client: Any
    progress: Callable[[str], AbstractContextManager] = _null_progress
    origin: str = ""

    def project_request(self, *, system, messages, tools, model):
        return {'system': system, 'messages': anthropic_history(messages, origin=self.origin, model=model), 'tools': tools}

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
        if thinking is not None:
            request["thinking"] = thinking
        chunks = []
        try:
            with (
                self.progress("Working") as progress,
                self.client.messages.stream(**request) as stream,
            ):
                    for chunk in stream.text_stream:
                        if not chunk:
                            continue
                        if not chunks:
                            progress.stop()
                        chunks.append(chunk)
                        print(chunk, end="", flush=True)
                    return scope_anthropic(stream.get_final_message(), self.origin, model)
        except Exception as exc:
            if chunks:
                raise PartialStreamError("".join(chunks), exc) from exc
            raise
        finally:
            if chunks and not chunks[-1].endswith("\n"):
                print()
