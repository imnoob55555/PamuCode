"""Anthropic SDK request and streaming boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, ContextManager

from ..core.recovery import PartialStreamError


class _NullProgress:
    def __enter__(self):
        return self

    def stop(self) -> None:
        pass

    def __exit__(self, exc_type, exc, traceback):
        return False


def _null_progress(_label: str) -> ContextManager:
    return _NullProgress()


@dataclass(frozen=True, slots=True)
class AnthropicAdapter:
    client: Any
    progress: Callable[[str], ContextManager] = _null_progress

    def create(
        self,
        *,
        system,
        messages,
        model,
        max_tokens,
        tools,
    ):
        request = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if system is not None:
            request["system"] = system
        if tools is not None:
            request["tools"] = tools
        return self.client.messages.create(**request)

    def create_streaming(
        self,
        *,
        system,
        messages,
        model,
        max_tokens,
        tools,
    ):
        chunks = []
        try:
            with self.progress("Working") as progress:
                with self.client.messages.stream(
                    model=model,
                    system=system,
                    messages=messages,
                    tools=tools,
                    max_tokens=max_tokens,
                ) as stream:
                    for chunk in stream.text_stream:
                        if not chunk:
                            continue
                        if not chunks:
                            progress.stop()
                        chunks.append(chunk)
                        print(chunk, end="", flush=True)
                    return stream.get_final_message()
        except Exception as exc:
            if chunks:
                raise PartialStreamError("".join(chunks), exc) from exc
            raise
        finally:
            if chunks and not chunks[-1].endswith("\n"):
                print()
