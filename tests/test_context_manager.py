import json
from types import SimpleNamespace

import pytest

from agent_app.bootstrap import build_runtime
from agent_app.config import AppConfig
from agent_app.core.context_manager import compact, context_report, context_control
from agent_app.adapters.common import ModelResponse, TextBlock


class Summarizer:
    def __init__(self, text):
        self.text = text
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return ModelResponse([TextBlock(self.text)], 'end_turn')


def runtime(tmp_path, text):
    adapter = Summarizer(text)
    rt = build_runtime(AppConfig.from_env(tmp_path, environ={'MODEL_ID': 'test'}), None, adapter=adapter)
    for i in range(6):
        rt.session.history.extend([{'role': 'user', 'content': f'requirement {i}'},
                                   {'role': 'assistant', 'content': 'evidence ' * 1000}])
    return rt


def test_compact_preserves_current_user_and_focus_and_restores(tmp_path):
    summary = dict(goal='fix', constraints=[], decisions=[], completed=[], remaining=['test'], uncertainties=[], references=[])
    rt = runtime(tmp_path, json.dumps(summary))
    try:
        assert compact(rt, focus='keep test evidence')
        assert any(m['content'] == 'requirement 5' for m in rt.session.history)
        assert 'keep test evidence' in str(rt.llm.requests)
        assert rt.session.context_state['generation'] == 1
        assert '估算' in context_report(rt)
    finally:
        rt.sessions.close()


@pytest.mark.parametrize(('response', 'reason'), [
    (ModelResponse([TextBlock('not json')], 'end_turn'), '模型未返回有效 JSON'),
    (ModelResponse([TextBlock('```json')], 'end_turn'), 'JSON 代码块不完整'),
    (ModelResponse([], 'max_tokens'), '达到 token 上限'),
])
def test_invalid_summary_does_not_replace_history(tmp_path, response, reason):
    rt = runtime(tmp_path, '')
    before = list(rt.session.history)
    calls = []

    def create(**kwargs):
        calls.append(kwargs['messages'][0]['content'])
        return response

    rt.llm.create = create
    try:
        with pytest.raises(ValueError, match=reason):
            compact(rt)
        assert rt.session.history == before
        assert len(calls) == 2 and 'Retry with compact, valid JSON' in calls[1]
    finally:
        rt.sessions.close()


def test_deepseek_resume_sends_the_turn_without_premature_compaction(tmp_path, capsys):
    from dataclasses import replace
    from agent_app.adapters.providers import build_adapter, get_provider
    from agent_app.cli import run_agent_turn
    from agent_app.core.context_manager import estimate_current_context, register_history_tools
    from agent_app.features.sessions import checkpoint

    calls = []

    class Stream:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        text_stream = ('继续成功',)

        def get_final_message(self):
            return ModelResponse([TextBlock('继续成功')], 'end_turn')

    def create(**kwargs):
        calls.append(('summary', kwargs))
        return ModelResponse([], 'max_tokens')

    def stream(**kwargs):
        calls.append(('turn', kwargs))
        return Stream()

    model = 'deepseek-v4-pro'
    url = 'https://api.deepseek.com/anthropic'
    # This session was saved under the Anthropic-compatible provider name.
    adapter = build_adapter(get_provider('anthropic'),
                            SimpleNamespace(messages=SimpleNamespace(create=create, stream=stream)), base_url=url)
    config = replace(AppConfig.from_env(tmp_path, environ={'MODEL_ID': model}), headless=True)
    original = build_runtime(config, None, adapter=adapter, base_url=url)
    for i in range(6):
        original.session.history.extend([{'role': 'user', 'content': f'需求 {i}'},
                                         {'role': 'assistant', 'content': '工作结果 ' * 100}])
    checkpoint(original)
    store = original.sessions.store
    snapshot = store.load(original.sessions.session_id)
    original.sessions.close()

    rt = build_runtime(config, None, adapter=adapter, base_url=url)
    try:
        rt.sessions.restore(rt, snapshot, store.acquire(snapshot['id']))
        register_history_tools(rt)
        assert 9174 < estimate_current_context(rt).input_tokens < 30_000
        run_agent_turn(rt, '继续上次任务')
        assert [kind for kind, _ in calls] == ['turn']
        assert calls[0][1]['thinking'] == {'type': 'disabled'}
        assert '压缩摘要无效' not in capsys.readouterr().out
    finally:
        rt.sessions.close()


def test_deepseek_compaction_disables_default_thinking(tmp_path):
    from contextlib import contextmanager
    from agent_app.adapters.anthropic import AnthropicAdapter

    summary = dict(goal='fix', constraints=[], decisions=[], completed=[], remaining=[], uncertainties=[], references=[])
    rt = runtime(tmp_path, '')
    calls = []
    stages = []

    @contextmanager
    def progress(label):
        stages.append(label)
        yield

    def create(**kwargs):
        calls.append(kwargs)
        if kwargs.get('thinking') != {'type': 'disabled'}:
            return SimpleNamespace(stop_reason='max_tokens', content=[
                {'type': 'thinking', 'thinking': 'used the output budget', 'signature': 'sig'},
            ])
        return ModelResponse([TextBlock(json.dumps(summary))], 'end_turn')

    url = 'https://api.deepseek.com/anthropic'
    adapter = AnthropicAdapter(SimpleNamespace(messages=SimpleNamespace(create=create)), progress, origin=f'anthropic@{url}')
    rt.model.switch('anthropic', 'deepseek-v4-pro', llm=adapter, protocol='anthropic_messages', base_url=url)
    try:
        assert compact(rt)
        assert calls and all(call['thinking'] == {'type': 'disabled'} for call in calls)
        assert rt.session.context_state['generation'] == 1
        assert stages == ['正在压缩上下文'] * len(calls)
    finally:
        rt.sessions.close()


def test_pins_are_persisted_without_model_call(tmp_path):
    rt = runtime(tmp_path, '')
    try:
        context_control(rt, 'pin preserve compatibility')
        assert rt.session.context_state['pins'][0]['text'] == 'preserve compatibility'
        assert not rt.llm.requests
    finally:
        rt.sessions.close()


def test_repl_context_usage_refreshes_for_active_provider_and_model(tmp_path):
    from agent_app.adapters.anthropic import AnthropicAdapter
    from agent_app.adapters.ollama import OllamaAdapter
    from agent_app.adapters.openai_chat import OpenAIChatAdapter
    from agent_app.adapters.openai_responses import OpenAIResponsesAdapter
    from agent_app.cli import main

    targets = [
        ('anthropic', 'anthropic_messages', 'https://api.anthropic.com',
         'claude-sonnet-4-6', AnthropicAdapter, 200_000),
        ('openai', 'openai_responses', 'https://api.openai.com/v1',
         'responses-model', OpenAIResponsesAdapter, 128_000),
        ('openai-compatible', 'openai_chat', 'https://gateway.test/v1',
         'chat-model', OpenAIChatAdapter, 65_536),
        ('ollama', 'ollama', 'http://localhost:11434',
         'local-model', OllamaAdapter, 4096),
    ]
    config = AppConfig.from_env(tmp_path, environ={'MODEL_ID': targets[0][3]})
    rt = build_runtime(config, None)
    rt.session.history.append({'role': 'user', 'content': 'Analyze this context. ' * 400})
    for _, protocol, origin, model, _, window in targets[1:3]:
        context_key = '|'.join((protocol, origin, model))
        rt.session.context_state.setdefault('window_overrides', {})[context_key] = window

    def select(target):
        provider, protocol, origin, model, adapter, _ = target
        rt.model.switch(provider, model, llm=adapter(None, origin=origin),
                        protocol=protocol, base_url=origin)

    readings = []

    def prompt():
        readings.append(ui.context_used)
        if len(readings) == len(targets):
            return '/quit'
        select(targets[len(readings)])
        return '/help'

    ui = SimpleNamespace(prompt=prompt)
    select(targets[0])
    try:
        main(argv=[], ui=ui, runtime_factory=lambda: rt,
             start_threads=lambda _: [], stop_threads=lambda *_: None,
             credentials_path=tmp_path / 'credentials.json')
        assert 0 < readings[0] < readings[1] < readings[2] < readings[3]
    finally:
        rt.sessions.close()


def test_context_slash_pins_without_model_and_updates_help(tmp_path):
    from agent_app.cli import handle_slash_command
    from agent_app.slash_commands import match_commands
    rt = runtime(tmp_path, '')
    output = []
    try:
        handle_slash_command(rt, '/context pin keep ABI', credentials_path=tmp_path/'credentials.json', print_fn=output.append)
        assert rt.session.context_state['pins'][0]['text'] == 'keep ABI'
        assert any('已固定' in item for item in output)
        assert not rt.llm.requests
        assert {c.name for c in match_commands('/')} >= {'/resume', '/compact', '/context'}
    finally:
        rt.sessions.close()


def test_multibyte_summary_chunks_cover_tail_within_actual_budget(tmp_path):
    from agent_app.core.context_budget import prepare_budget
    summary = dict(goal='fix', constraints=[], decisions=[], completed=[], remaining=[], uncertainties=[], references=[])
    rt = runtime(tmp_path, json.dumps(summary))
    rt.session.history = [{'role':'user','content':'old'}, {'role':'assistant','content':'中'*9000+'END_EVIDENCE'}, {'role':'user','content':'middle'}, {'role':'assistant','content':'x'*100}, {'role':'user','content':'latest'}]
    try:
        assert compact(rt)
        assert 'END_EVIDENCE' in ''.join(r['messages'][0]['content'] for r in rt.llm.requests)
        assert len(rt.llm.requests) <= 8
        for r in rt.llm.requests:
            report = prepare_budget(rt, r['system'], r['messages'], r['tools'], r['max_tokens'])
            assert report.remaining >= 0
    finally:
        rt.sessions.close()


def test_compact_does_not_repeat_without_new_interaction(tmp_path):
    summary = dict(goal='fix', constraints=[], decisions=[], completed=[], remaining=[], uncertainties=[], references=[])
    rt = runtime(tmp_path, json.dumps(summary))
    try:
        assert compact(rt)
        requests = len(rt.llm.requests)
        assert compact(rt) is False
        assert len(rt.llm.requests) == requests
    finally:
        rt.sessions.close()


def test_summary_request_limit_leaves_original_history(tmp_path):
    summary = dict(goal='fix', constraints=[], decisions=[], completed=[], remaining=[], uncertainties=[], references=[])
    rt = runtime(tmp_path, json.dumps(summary))
    rt.session.history[1]['content'] = '中' * 200000
    before = list(rt.session.history)
    try:
        with pytest.raises(ValueError, match='上限'):
            compact(rt)
        assert rt.session.history == before
        assert len(rt.llm.requests) == 8
    finally:
        rt.sessions.close()


def test_hundred_small_records_pack_without_losing_source(tmp_path):
    summary = dict(goal='fix', constraints=[], decisions=[], completed=[], remaining=[], uncertainties=[], references=[])
    rt = runtime(tmp_path, json.dumps(summary))
    rt.session.history = [{'role':'user' if i % 2 == 0 else 'assistant', 'content': f'UNIQUE_RECORD_{i:03d}_END'} for i in range(100)]
    try:
        assert compact(rt)
        prompts = ''.join(r['messages'][0]['content'] for r in rt.llm.requests)
        retained = str(rt.session.history)
        for i in range(100):
            assert f'UNIQUE_RECORD_{i:03d}_END' in prompts or f'UNIQUE_RECORD_{i:03d}_END' in retained
        assert len(rt.llm.requests) < 8
    finally:
        rt.sessions.close()



def test_large_reference_catalog_is_distributed_across_bounded_requests(tmp_path):
    summary = dict(goal='fix', constraints=[], decisions=[], completed=[], remaining=[], uncertainties=[], references=[])
    rt = runtime(tmp_path, json.dumps(summary))
    rt.session.history = [{'role':'user' if i % 2 == 0 else 'assistant', 'content': f'RECORD_{i:04d}'} for i in range(500)]
    try:
        assert compact(rt)
        assert len(rt.llm.requests) <= 8
        for request in rt.llm.requests:
            assert 'Allowed references: [' not in request['messages'][0]['content']
    finally:
        rt.sessions.close()


def test_native_summary_source_reference_is_readable_after_resume(tmp_path):
    import re
    from agent_app.features.context_archive import ContextArchive
    from agent_app.features.session_store import SessionStore
    summary = dict(goal='fix', constraints=[], decisions=[], completed=[], remaining=[], uncertainties=[], references=[])
    rt = runtime(tmp_path, json.dumps(summary))
    rt.session.history[1]['content'] = [
        {'type':'thinking','thinking':'HIDDEN_REASONING','signature':'sig','_pamu_scope':('anthropic_messages','','test')},
        {'type':'text','text':'VISIBLE_NATIVE_EVIDENCE ' * 500},
    ]
    cumulative_refs = []
    def summarize(**kwargs):
        refs = re.findall(r'Source reference (sha256:[0-9a-f]{64})', kwargs['messages'][0]['content'])
        cumulative_refs.extend(refs)
        answer = dict(summary, references=list(dict.fromkeys(cumulative_refs)))
        return ModelResponse([TextBlock(json.dumps(answer))], 'end_turn')
    rt.llm.create = summarize
    try:
        assert compact(rt)
        sid = rt.sessions.session_id
        cited = rt.session.context_state['summary']['references']
        rt.sessions.close()
        fresh = runtime(tmp_path, json.dumps(summary))
        store = SessionStore(tmp_path)
        fresh.sessions.restore(fresh, store.load(sid), store.acquire(sid))
        try:
            texts = [str(ContextArchive(tmp_path, sid).read(ref)) for ref in cited]
            assert any('VISIBLE_NATIVE_EVIDENCE' in text for text in texts)
            assert all('HIDDEN_REASONING' not in text for text in texts)
        finally:
            fresh.sessions.close()
    finally:
        rt.sessions.close()


def test_summary_adapter_uses_resolved_context_window(tmp_path):
    from dataclasses import replace
    summary = dict(goal='fix', constraints=[], decisions=[], completed=[], remaining=[], uncertainties=[], references=[])
    rt = runtime(tmp_path, json.dumps(summary))
    rt.config = replace(rt.config, context_window=65536, context_window_target=None)
    selected = []
    original_create = rt.llm.create
    def with_context_window(window):
        selected.append(window)
        return SimpleNamespace(create=original_create)
    rt.llm.with_context_window = with_context_window
    try:
        assert compact(rt)
        assert selected and set(selected) == {65536}
    finally:
        rt.sessions.close()


@pytest.mark.parametrize('entry', ['tool', 'history'])
@pytest.mark.parametrize('fail_save', [False, True])
def test_large_results_keep_full_evidence_and_publish_only_after_save(tmp_path, monkeypatch, entry, fail_save):
    import re
    from agent_app.core.context_manager import archive_for, normalize_results
    from agent_app.core.loop import run_agent_loop
    from tests.agent_app_fakes import FakeAdapter, text_response, tool_response, tool_schema

    evidence = 'BEGIN_EVIDENCE ' + '中' * 12000 + ' END_EVIDENCE'
    rt = runtime(tmp_path, '')
    rt.session.history[:] = [{'role': 'user', 'content': 'read evidence'}]
    archive = archive_for(rt)
    if fail_save:
        def reject_save(text):
            raise OSError('disk full')
        monkeypatch.setattr(archive, 'save_artifact', reject_save)
    if entry == 'tool':
        adapter = FakeAdapter([tool_response('c1', 'evidence', {}), text_response('done')])
        rt.llm = rt.model.llm = adapter
        rt.tools.register(tool_schema('evidence'), lambda: evidence)
        run = lambda: run_agent_loop(rt)
    else:
        rt.session.history.extend([
            {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'c1', 'name': 'evidence', 'input': {}}]},
            {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'c1', 'content': evidence}]},
        ])
        run = lambda: normalize_results(rt, rt.session.history, 1000)
    try:
        if fail_save:
            with pytest.raises(OSError, match='disk full'):
                run()
            assert '[归档结果 ' not in str(rt.session.history)
            if entry == 'history':
                assert rt.session.history[-1]['content'][0]['content'] == evidence
        else:
            run()
            result = next(b['content'] for m in rt.session.history if isinstance(m['content'], list)
                          for b in m['content'] if isinstance(b, dict) and b.get('type') == 'tool_result')
            ref = re.search(r'\[归档结果 (artifact:[0-9a-f]{64})\]', result).group(1)
            assert len(result) < len(evidence)
            assert 'BEGIN_EVIDENCE' in result and 'END_EVIDENCE' in result
            # The committed snapshot and complete artifact remain readable after reopening.
            sid = rt.sessions.session_id
            rt.sessions.close()
            saved = rt.sessions.store.load(sid)
            assert ref in str(saved['history'])
            assert archive._read(ref) == evidence
            assert not normalize_results(rt, rt.session.history, 1000)
    finally:
        rt.sessions.close()
