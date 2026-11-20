from types import SimpleNamespace as NS
import pytest


def test_budget_reserves_output_and_safety():
    from agent_app.core.context_budget import ContextBudget
    b = ContextBudget(4096, 8000)
    assert b.output_tokens == 1024
    assert b.input_limit == 2560
    assert b.trigger == 2048 and b.target == 1536
    with pytest.raises(ValueError): ContextBudget(100, 1)


def test_usage_cache_and_unknown():
    from agent_app.core.context_budget import extract_usage
    assert extract_usage({}, 'openai_chat') is None
    a = extract_usage({'usage': {'input_tokens': 10, 'output_tokens': 2, 'cache_read_input_tokens': 30, 'cache_creation_input_tokens': 20}}, 'anthropic_messages')
    assert a.input_tokens == 60 and a.output_tokens == 2
    c = extract_usage({'usage': {'prompt_tokens': 50, 'completion_tokens': 3, 'prompt_tokens_details': {'cached_tokens': 40}}}, 'openai_chat')
    assert c.input_tokens == 50 and c.cache_read_tokens == 40


def test_windows_bound_to_startup_target_and_exact_origin():
    from agent_app.core.context_budget import resolve_window
    r = NS(config=NS(primary_model='a', context_window=32000), model=NS(primary_model='a', protocol='anthropic_messages', base_url='https://gateway.test', llm=None), session=NS(context_state={}))
    assert resolve_window(r)[0] == 32000
    assert resolve_window(r, 'b')[0] == 16384
    r.config.context_window = None
    r.model.primary_model = 'claude-sonnet-4-6'
    assert resolve_window(r)[0] == 16384
    r.model.base_url = 'https://api.anthropic.com'
    assert resolve_window(r)[0] == 200000


def test_deepseek_official_window_yields_to_explicit_overrides():
    from agent_app.core.context_budget import resolve_window
    origin = 'https://api.deepseek.com/anthropic'
    model = 'deepseek-v4-pro'
    r = NS(config=NS(primary_model=model, context_window=None),
           model=NS(primary_model=model, protocol='anthropic_messages', base_url=origin, llm=None),
           session=NS(context_state={}))
    assert resolve_window(r)[0] == 1_000_000
    assert resolve_window(r, 'unknown')[0] == 16_384
    r.session.context_state['window_overrides'] = {f'anthropic_messages|{origin}|{model}': 32_000}
    assert resolve_window(r)[0] == 32_000
    r.config.context_window = 64_000
    r.config.context_window_target = ('anthropic_messages', origin, model)
    assert resolve_window(r)[0] == 64_000
    r.model.base_url = 'https://gateway.test/anthropic'
    assert resolve_window(r)[0] == 16_384


def test_estimate_strips_metadata_and_counts_tools():
    from agent_app.core.context_budget import estimate_request
    m = [{'role': 'user', 'content': '你好'}]
    n = [{'role': 'user', 'content': '你好', '_pamu_id': 'x' * 100000}]
    assert estimate_request('rules', m, []) == estimate_request('rules', n, [])
    assert estimate_request('rules', m, [{'name':'x','input_schema':{'description':'x'*5000}}]) > estimate_request('rules', m, [])


def test_ollama_context_and_usage():
    from agent_app.adapters.ollama import OllamaAdapter
    adapter = OllamaAdapter(None).with_context_window(8192)
    assert adapter._request('', [], 'a', 10, [], None)['options']['num_ctx'] == 8192
    result = adapter._response({'done': True, 'message': {'content': 'ok'}, 'prompt_eval_count': 40, 'eval_count': 2}, 'a')
    assert result.usage.input_tokens == 40


def test_projection_and_calibration_are_target_local():
    from agent_app.core.context_budget import prepare_budget, record_usage
    class Adapter:
        def project_request(self, **kwargs):
            return {'native': 'x' * 2000}
    r = NS(config=NS(primary_model='a', context_window=None), model=NS(primary_model='a', protocol='openai_chat', base_url='https://gateway', llm=Adapter()), session=NS(context_state={}), llm=None)
    first = prepare_budget(r, '', [], [], 8000)
    record_usage(r, {'usage': {'prompt_tokens': first.input_tokens * 2, 'completion_tokens': 2}}, estimated_input=first.input_tokens)
    assert prepare_budget(r, '', [], [], 8000).input_tokens == first.input_tokens * 2
    assert prepare_budget(r, '', [], [], 8000, model='b').input_tokens == first.input_tokens
    for _ in range(10):
        record_usage(r, {'usage': {'prompt_tokens': 1}}, estimated_input=first.input_tokens)
    assert len(next(iter(r.session.context_state['usage_ratios'].values()))) == 8
    assert prepare_budget(r, '', [], [], 8000).input_tokens == first.input_tokens


def test_delegated_budget_stops_oversized_request():
    from agent_app.core.delegated_context import DelegatedContext
    from agent_app.config import AppConfig
    from pathlib import Path
    config = AppConfig.from_env(Path('/tmp'), environ={'MODEL_ID': 'a'})
    calls = []
    llm = lambda **kwargs: calls.append(kwargs)
    d = DelegatedContext(config, llm, 'a')
    with pytest.raises(ValueError, match='上下文'):
        d.request(llm, system='x' * 100000, messages=[{'role':'user','content':'task'}], tools=[], max_tokens=8000)
    assert calls == []


def test_anthropic_projection_keeps_only_native_scope_sent_to_model():
    from agent_app.adapters.anthropic import AnthropicAdapter
    adapter = AnthropicAdapter(None, origin='https://api.anthropic.com')
    blocks = [{'type':'thinking','thinking':'PRIVATE','signature':'sig','_pamu_scope':('anthropic_messages','https://api.anthropic.com','m')}, {'type':'text','text':'answer'}]
    projected = adapter.project_request(system='rules', messages=[{'role':'assistant','content':blocks,'_pamu_id':'secret_internal'}], tools=[], model='m')
    assert projected['messages'][0]['content'][0]['thinking'] == 'PRIVATE'
    assert '_pamu_id' not in str(projected)
    assert 'PRIVATE' not in str(adapter.project_request(system=None,messages=[{'role':'assistant','content':blocks}],tools=[],model='other'))
