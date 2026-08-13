from types import SimpleNamespace

import pytest

from agent_app.adapters.anthropic import AnthropicAdapter
from agent_app.core.recovery import PartialStreamError


class RecordingProgress:
    def __init__(self, events):
        self.events = events
        self.stopped = False

    def __enter__(self):
        self.events.append("enter")
        return self

    def stop(self):
        if self.stopped:
            return
        self.stopped = True
        self.events.append("stop")

    def __exit__(self, exc_type, exc, traceback):
        self.stop()
        self.events.append("exit")
        return False


def test_adapter_forwards_non_streaming_request_body():
    final_message = object()
    captured = {}

    def create(**kwargs):
        captured.update(kwargs)
        return final_message

    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    adapter = AnthropicAdapter(client)

    result = adapter.create(
        system="system",
        messages=[{"role": "user", "content": "hello"}],
        model="model",
        max_tokens=100,
        tools=[{"name": "echo"}],
    )

    assert result is final_message
    assert captured == {
        "model": "model",
        "system": "system",
        "messages": [{"role": "user", "content": "hello"}],
        "tools": [{"name": "echo"}],
        "max_tokens": 100,
    }


def test_streaming_adapter_wraps_failure_after_visible_text(capsys):
    cause = RuntimeError("lost")

    class FailingStream:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        @property
        def text_stream(self):
            def chunks():
                yield "visible"
                raise cause

            return chunks()

        def get_final_message(self):
            raise AssertionError("failed stream has no final message")

    client = SimpleNamespace(
        messages=SimpleNamespace(
            stream=lambda **_kwargs: FailingStream(),
        )
    )
    adapter = AnthropicAdapter(client)

    with pytest.raises(PartialStreamError) as caught:
        adapter.create_streaming(
            system="system",
            messages=[],
            model="model",
            max_tokens=100,
            tools=[],
        )

    assert caught.value.partial_text == "visible"
    assert caught.value.cause is cause
    assert capsys.readouterr().out == "visible\n"


def test_streaming_adapter_returns_final_message_after_printing_chunks(capsys):
    final_message = object()
    captured = {}

    class SuccessfulStream:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        @property
        def text_stream(self):
            return iter(["visible", " text"])

        def get_final_message(self):
            return final_message

    def stream(**kwargs):
        captured.update(kwargs)
        return SuccessfulStream()

    client = SimpleNamespace(messages=SimpleNamespace(stream=stream))
    adapter = AnthropicAdapter(client)

    result = adapter.create_streaming(
        system="system",
        messages=[],
        model="model",
        max_tokens=100,
        tools=[],
    )

    assert result is final_message
    assert captured == {
        "model": "model",
        "system": "system",
        "messages": [],
        "tools": [],
        "max_tokens": 100,
    }
    assert capsys.readouterr().out == "visible text\n"


def test_streaming_stops_progress_before_first_visible_chunk(monkeypatch):
    events = []
    final_message = object()

    class SuccessfulStream:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        @property
        def text_stream(self):
            return iter(["", "visible"])

        def get_final_message(self):
            return final_message

    client = SimpleNamespace(
        messages=SimpleNamespace(stream=lambda **_kwargs: SuccessfulStream())
    )
    adapter = AnthropicAdapter(
        client,
        progress=lambda _label: RecordingProgress(events),
    )
    monkeypatch.setattr(
        "builtins.print",
        lambda value="", **_kwargs: events.append(f"print:{value}"),
    )

    result = adapter.create_streaming(
        system="system",
        messages=[],
        model="model",
        max_tokens=100,
        tools=[],
    )

    assert result is final_message
    assert events[:4] == ["enter", "stop", "print:visible", "exit"]


def test_streaming_exception_exits_progress(monkeypatch):
    events = []
    cause = RuntimeError("lost")

    class FailingStream:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        @property
        def text_stream(self):
            raise cause

    client = SimpleNamespace(
        messages=SimpleNamespace(stream=lambda **_kwargs: FailingStream())
    )
    adapter = AnthropicAdapter(
        client,
        progress=lambda _label: RecordingProgress(events),
    )
    monkeypatch.setattr(
        "builtins.print",
        lambda value="", **_kwargs: events.append(f"print:{value}"),
    )

    with pytest.raises(RuntimeError, match="lost"):
        adapter.create_streaming(
            system="system",
            messages=[],
            model="model",
            max_tokens=100,
            tools=[],
        )

    assert events == ["enter", "stop", "exit"]


def test_tool_only_stream_keeps_progress_until_final_message():
    events = []
    final_message = object()

    class ToolOnlyStream:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        @property
        def text_stream(self):
            return iter(())

        def get_final_message(self):
            events.append("final")
            return final_message

    client = SimpleNamespace(
        messages=SimpleNamespace(stream=lambda **_kwargs: ToolOnlyStream())
    )
    adapter = AnthropicAdapter(
        client,
        progress=lambda _label: RecordingProgress(events),
    )

    result = adapter.create_streaming(
        system="system",
        messages=[],
        model="model",
        max_tokens=100,
        tools=[],
    )

    assert result is final_message
    assert events == ["enter", "final", "stop", "exit"]
