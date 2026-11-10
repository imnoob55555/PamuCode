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


def test_invalid_summary_does_not_replace_history(tmp_path):
    rt = runtime(tmp_path, 'not json')
    before = list(rt.session.history)
    try:
        with pytest.raises(ValueError):
            compact(rt)
        assert rt.session.history == before
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
