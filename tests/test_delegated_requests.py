"""Delegated requests own preparation, retries and successful usage records."""

from copy import deepcopy
from dataclasses import replace
import json

import pytest

from agent_app.adapters.common import ModelResponse, TextBlock
from agent_app.config import AppConfig
from agent_app.core.context_budget import ModelUsage
from agent_app.core.delegated_context import DelegatedContext
from agent_app.core.recovery import PartialStreamError


class Overloaded(Exception):
    status_code = 529


def response(text='done'):
    return ModelResponse([TextBlock(text)], 'end_turn', ModelUsage(100, 2))


@pytest.fixture
def config(tmp_path):
    return replace(
        AppConfig.from_env(tmp_path, environ={'MODEL_ID': 'primary', 'PAMU_CONTEXT_WINDOW': '32768'}),
        max_transient_retries=3, max_consecutive_529=1, base_delay_ms=0,
    )


@pytest.mark.parametrize('include_model', [False, True])
def test_callable_injection_records_once_and_copies_outgoing_history(config, include_model):
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        kwargs['messages'].append({'role': 'assistant', 'content': 'adapter-local'})
        return response()

    delegate = DelegatedContext(config, create, 'primary')
    history = [{'role': 'user', 'content': 'inspect'}]
    selection = {'model': 'primary'} if include_model else {}
    for _ in range(2):
        assert delegate.request(create, system=None, messages=history, tools=[], max_tokens=100,
                                **selection).content[0].text == 'done'

    assert history == [{'role': 'user', 'content': 'inspect'}]
    assert all(call['messages'] is not history for call in calls)
    assert all(('model' in call) == include_model for call in calls)
    state = delegate.runtime.session.context_state
    assert state['last_usage']['target'][-1] == 'primary'
    assert [len(samples) for samples in state['usage_ratios'].values()] == [2]


class WindowAdapter:
    def __init__(self, calls=None, window=None):
        self.calls = [] if calls is None else calls
        self.window = window

    def with_context_window(self, window):
        return type(self)(self.calls, window)

    def create(self, **kwargs):
        self.calls.append({'window': self.window, **kwargs})
        if kwargs['model'] == 'primary':
            raise Overloaded()
        return response()


def test_fallback_rebudgets_each_attempt_and_retains_its_usage_target(config):
    adapter = WindowAdapter()
    delegate = DelegatedContext(config, adapter.create, 'primary', protocol='ollama',
                                origin='http://local.test', adapter=adapter, fallback_model='fallback')
    history = [{'role': 'user', 'content': 'inspect'}]
    for _ in range(2):
        delegate.request(adapter.create, system=None, messages=history, tools=[], max_tokens=8000,
                         model='primary')

    assert [(call['model'], call['window'], call['max_tokens']) for call in adapter.calls] == [
        ('primary', 32768, 8000), ('fallback', 4096, 1024), ('fallback', 4096, 1024),
    ]
    state = delegate.runtime.session.context_state
    assert state['last_usage']['target'] == ['ollama', 'http://local.test', 'fallback']
    assert list(state['usage_ratios']) == [json.dumps(('ollama', 'http://local.test', 'fallback'))]
    assert len(next(iter(state['usage_ratios'].values()))) == 2


def test_fallback_overflow_is_rejected_before_sending_request(config):
    adapter = WindowAdapter()
    delegate = DelegatedContext(config, adapter.create, 'primary', protocol='ollama',
                                adapter=adapter, fallback_model='fallback')
    with pytest.raises(ValueError, match='上下文'):
        delegate.request(adapter.create, system='rules ' * 2000,
                         messages=[{'role': 'user', 'content': 'inspect'}], tools=[], max_tokens=8000)
    assert [call['model'] for call in adapter.calls] == ['primary']
    assert 'last_usage' not in delegate.runtime.session.context_state


def test_retry_limits_and_delay_come_from_delegate_configuration(config, monkeypatch):
    from agent_app.core import recovery

    calls, delays = [], []
    config = replace(config, max_transient_retries=2, base_delay_ms=1200)
    monkeypatch.setattr(recovery.time, 'sleep', delays.append)
    monkeypatch.setattr(recovery.random, 'uniform', lambda low, high: 0)

    def create(**kwargs):
        calls.append(kwargs)
        raise Overloaded()

    delegate = DelegatedContext(config, create, 'primary')
    with pytest.raises(Overloaded):
        delegate.request(create, system=None, messages=[], tools=[], max_tokens=100)
    assert len(calls) == 2
    assert delays == [1.2]


def test_fallback_compaction_is_published_to_caller_history(config):
    summary = json.dumps(dict(goal='inspect', constraints=[], decisions=[], completed=[],
                              remaining=['test'], uncertainties=[], references=[]))

    class CompactingAdapter(WindowAdapter):
        def create(self, **kwargs):
            if 'Summarize the coding task faithfully' in str(kwargs['messages']):
                self.calls.append({'window': self.window, **kwargs})
                return response(summary)
            return super().create(**kwargs)

    adapter = CompactingAdapter()
    delegate = DelegatedContext(config, adapter.create, 'primary',
                                adapter=adapter, fallback_model='fallback')
    history = []
    for index in range(6):
        history.extend([{'role': 'user', 'content': f'requirement {index}'},
                        {'role': 'assistant', 'content': 'evidence ' * 450}])
    delegate.request(adapter.create, system=None, messages=history, tools=[], max_tokens=1024)

    assert [adapter.calls[0]['model'], adapter.calls[-1]['model']] == ['primary', 'fallback']
    assert history[0]['content'].startswith('[Compacted]')
    assert {'role': 'user', 'content': 'requirement 5'} in history
    assert adapter.calls[-1]['messages'] == history
    state = delegate.runtime.session.context_state
    assert state['generation'] == 1
    assert state['last_usage']['target'][-1] == 'fallback'
    assert [len(samples) for samples in state['usage_ratios'].values()] == [1]


@pytest.mark.parametrize('error', [ValueError('invalid request'), PartialStreamError('visible', Overloaded())])
def test_non_retryable_failures_are_not_replayed_or_recorded(config, error):
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        raise error

    delegate = DelegatedContext(config, create, 'primary')
    with pytest.raises(type(error)) as raised:
        delegate.request(create, system=None, messages=[], tools=[], max_tokens=100)
    assert raised.value is error
    assert len(calls) == 1
    assert 'last_usage' not in delegate.runtime.session.context_state


@pytest.mark.parametrize('valid_summary', [True, False])
def test_compaction_updates_caller_history_only_after_valid_summary(config, valid_summary):
    summary = json.dumps(dict(goal='inspect', constraints=[], decisions=[], completed=[],
                              remaining=['test'], uncertainties=[], references=[]))
    summaries, requests = [], []

    def create(**kwargs):
        if 'Summarize the coding task faithfully' in str(kwargs['messages']):
            summaries.append(kwargs)
            return response(summary if valid_summary else 'invalid JSON')
        requests.append(kwargs)
        return response()

    delegate = DelegatedContext(config, create, 'primary')
    history = []
    for index in range(6):
        history.extend([{'role': 'user', 'content': f'requirement {index}'},
                        {'role': 'assistant', 'content': 'evidence ' * 1000}])
    before = deepcopy(history)

    def request():
        return delegate.request(create, system=None, messages=history, tools=[], max_tokens=1024,
                                model='primary')

    if valid_summary:
        request()
        assert history[0]['content'].startswith('[Compacted]')
        assert {'role': 'user', 'content': 'requirement 5'} in history
        assert requests[0]['messages'] == history
        count = len(summaries)
        request()
        assert len(summaries) == count
        assert delegate.runtime.session.context_state['generation'] == 1
    else:
        with pytest.raises(ValueError, match='压缩摘要无效'):
            request()
        assert history == before
        assert requests == []
        assert 'last_usage' not in delegate.runtime.session.context_state
    assert summaries
    assert delegate.runtime.session.history is history
