import json
import pytest
from pathlib import Path

from agent_app.execution import execute


def run(tmp_path, responses, **kwargs):
    replay = tmp_path / 'replay.json'
    replay.write_text(json.dumps(responses))
    return execute(task='do it', workdir=tmp_path, output=tmp_path/'out', model='test', protocol='anthropic_messages', replay=replay, max_model_calls=4, max_tool_calls=4, max_tokens=100, timeout=5, **kwargs)


def text(usage=None):
    return {'content':[{'type':'text','text':'done'}], 'stop_reason':'end_turn', 'usage':usage}


def test_real_write(tmp_path):
    response={'content':[{'type':'tool_use','id':'1','name':'write_file','input':{'path':'hello.txt','content':'hello'}}], 'stop_reason':'tool_use'}
    result=run(tmp_path,[response,text()])
    assert (tmp_path/'hello.txt').read_text()=='hello'
    assert result['status']=='completed'
    assert result['model_calls']==2
    assert result['tool_calls']==1
    assert result['usage']['total_tokens'] is None
    assert json.loads((tmp_path/'out/result.json').read_text())==result


def test_budget_and_policy(tmp_path):
    response={'content':[{'type':'tool_use','id':'1','name':'mcp_connect','input':{}}], 'stop_reason':'tool_use'}
    assert run(tmp_path,[response])['status']=='policy_denied'


def test_token_budget(tmp_path):
    result=run(tmp_path,[text({'input_tokens':101,'output_tokens':1})])
    assert result['status']=='budget_exceeded'


def test_replay_exhaustion_is_error(tmp_path):
    assert run(tmp_path,[])['status']=='error'


def test_no_home_config_and_known_usage(tmp_path, monkeypatch):
    from agent_app import bootstrap
    from agent_app.features import auth
    def forbidden(*args, **kwargs):
        raise AssertionError('personal configuration read')
    monkeypatch.setattr(bootstrap, 'load_runtime_environment', forbidden)
    monkeypatch.setattr(auth, 'load_credentials', forbidden)
    monkeypatch.setattr('builtins.input', forbidden)
    result = run(tmp_path, [text({'input_tokens': 5, 'output_tokens': 3})])
    assert result['status'] == 'completed'
    assert result['usage'] == {'input_tokens': 5, 'output_tokens': 3, 'total_tokens': 8, 'complete': True}


def test_cli_stdout_json(tmp_path, monkeypatch, capsys):
    from agent_app.cli import main
    monkeypatch.chdir(tmp_path)
    (tmp_path/'task').write_text('hello')
    (tmp_path/'replay').write_text(json.dumps([text()]))
    assert main(argv=['run', '--task-file', 'task', '--output', 'out', '--model', 'test', '--protocol', 'anthropic_messages', '--replay', 'replay']) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'completed'


def test_compact_denied_before_special_dispatch(tmp_path):
    response = {'content': [{'type': 'tool_use', 'id': '1', 'name': 'compact', 'input': {}}], 'stop_reason': 'tool_use'}
    result = run(tmp_path, [response])
    assert result['status'] == 'policy_denied'
    assert result['tool_calls'] == 1
    assert result['model_calls'] == 1


def test_tool_and_model_budgets(tmp_path):
    response = {'content': [{'type': 'tool_use', 'id': str(i), 'name': 'glob', 'input': {'pattern': '*'}} for i in range(2)], 'stop_reason': 'tool_use'}
    replay = tmp_path/'r.json'
    replay.write_text(json.dumps([response]))
    common = dict(task='x', workdir=tmp_path, model='test', protocol='anthropic_messages', replay=replay)
    result = execute(output=tmp_path/'tool', max_tool_calls=1, **common)
    assert result['stop_reason'] == 'max_tool_calls'
    response['content'] = response['content'][:1]
    replay.write_text(json.dumps([response]))
    result = execute(output=tmp_path/'model', max_model_calls=1, **common)
    assert result['stop_reason'] == 'max_model_calls'


def test_identical_tool_limit_has_structured_stop_reason(tmp_path):
    replay = tmp_path / 'replay.json'
    replay.write_text(json.dumps([
        {'content': [{'type': 'tool_use', 'id': str(i), 'name': 'glob', 'input': {'pattern': '*'}}],
         'stop_reason': 'tool_use'}
        for i in range(5)
    ]))
    result = execute(task='inspect', workdir=tmp_path, output=tmp_path / 'out', model='test',
                     protocol='anthropic_messages', replay=replay)
    assert result['status'] == 'budget_exceeded'
    assert result['stop_reason'] == 'repeated_tool_calls'
    assert result['tool_calls'] == 3


def test_auxiliary_budget_and_output_clamp(tmp_path):
    import pytest
    from agent_app.execution import TrackedAdapter, Ledger, ExecutionStop
    class Adapter:
        def create(self, **kwargs):
            assert kwargs['max_tokens'] == 7
            from agent_app.adapters.common import ModelResponse, TextBlock
            return ModelResponse([TextBlock('hi')], 'end_turn', {'input_tokens': 4, 'output_tokens': 3})
    ledger = Ledger(tmp_path, 1, 1, 7, 10)
    adapter = TrackedAdapter(Adapter(), ledger).with_context_window(1000)
    adapter.create(model='test', max_tokens=100)
    with pytest.raises(ExecutionStop):
        adapter.create_streaming(model='test', max_tokens=100)
    assert ledger.model_calls == 1


def test_bash_exit_stdin_and_cleanup(tmp_path):
    import time
    response = {'content': [{'type': 'tool_use', 'id': '1', 'name': 'bash', 'input': {'command': 'read value; printf hi; exit 3'}}], 'stop_reason': 'tool_use'}
    assert run(tmp_path, [response, text()])['status'] == 'completed'
    events = [json.loads(line) for line in (tmp_path/'out/events.jsonl').read_text().splitlines()]
    shell = json.loads(next(e['output'] for e in events if e['type'] == 'tool.completed'))
    assert shell['exit_code'] == 3
    assert shell['stdout'] == 'hi'
    replay = tmp_path/'r.json'
    response['content'][0]['input']['command'] = '(sleep .3; touch orphan) & sleep 20'
    replay.write_text(json.dumps([response]))
    result = execute(task='x', workdir=tmp_path, output=tmp_path/'timed', model='test', protocol='anthropic_messages', replay=replay, timeout=.1)
    assert result['stop_reason'] == 'timeout'
    time.sleep(.4)
    assert not (tmp_path/'orphan').exists()


def test_blocking_adapter_deadline(tmp_path, monkeypatch):
    import time
    from agent_app.execution import ReplayAdapter
    monkeypatch.setattr(ReplayAdapter, 'create', lambda *a, **k: time.sleep(10))
    replay = tmp_path/'r.json'
    replay.write_text('[]')
    result = execute(task='x', workdir=tmp_path, output=tmp_path/'out', model='test', protocol='anthropic_messages', replay=replay, timeout=.05)
    assert result['stop_reason'] == 'timeout'
    assert result['usage']['complete'] is False


def test_redacts_and_rejects_overwrite(tmp_path, monkeypatch):
    import pytest
    monkeypatch.setenv('TEST_API_KEY', 'secret-value')
    result = run(tmp_path, [{'content': [{'type': 'text', 'text': 'secret-value'}], 'stop_reason': 'end_turn'}])
    assert result['final_answer'] == '[REDACTED]'
    assert 'secret-value' not in (tmp_path/'out/events.jsonl').read_text()
    with pytest.raises(ValueError, match='evidence'):
        run(tmp_path, [text()])


def test_invalid_budgets(tmp_path):
    import pytest
    for kw in ({'timeout': float('nan')}, {'max_model_calls': 1.5}, {'max_tool_calls': 0}):
        with pytest.raises(ValueError):
            execute(task='x', workdir=tmp_path, output=tmp_path/'out', model='test', protocol='anthropic_messages', **kw)


@pytest.mark.parametrize('signal_name', ['SIGTERM', 'SIGINT'])
def test_user_signal_cleans_shell(tmp_path, signal_name):
    import os
    import signal
    import subprocess
    import sys
    import time
    project = Path(__file__).resolve().parents[1]
    (tmp_path/'task').write_text('test')
    (tmp_path/'replay').write_text(json.dumps([{'content': [{'type':'tool_use', 'id':'1', 'name':'bash', 'input':{'command':'touch started; (sleep .4; touch orphan) & sleep 20'}}], 'stop_reason':'tool_use'}]))
    proc = subprocess.Popen([sys.executable,'-m','agent_app','run','--task-file','task','--output','out','--model','test','--protocol','anthropic_messages','--replay','replay'],cwd=tmp_path,env={**os.environ,'PYTHONPATH':str(project)},stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        deadline = time.monotonic()+5
        while not (tmp_path/'started').exists() and time.monotonic()<deadline:
            time.sleep(.01)
        assert (tmp_path/'started').exists()
        proc.send_signal(getattr(signal, signal_name))
        stdout, stderr = proc.communicate(timeout=3)
        assert json.loads(stdout)['status'] == 'cancelled'
        time.sleep(.5)
        assert not (tmp_path/'orphan').exists()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_json_redaction_escapes_and_preserves_keys(tmp_path, monkeypatch):
    secret = 'quotes" and slash\\ and newline\n'
    monkeypatch.setenv('TEST_API_KEY', secret)
    result = run(tmp_path, [{'content': [{'type': 'text', 'text': secret}], 'stop_reason': 'end_turn'}])
    assert result['final_answer'] == '[REDACTED]'
    events = [json.loads(line) for line in (tmp_path/'out/events.jsonl').read_text().splitlines()]
    assert next(e for e in events if e['type'] == 'model.completed')['response']['content'][0]['text'] == '[REDACTED]'


def test_cli_bad_file_and_budget(tmp_path, capsys):
    from agent_app.execution import main
    common = ['--task-file', str(tmp_path/'missing'), '--output', str(tmp_path/'out'), '--model', 'test', '--protocol', 'anthropic_messages']
    assert main(common) == 2
    assert '运行配置错误' in capsys.readouterr().err
    (tmp_path/'missing').write_text('test')
    assert main(common + ['--timeout', 'nan']) == 2


def test_late_response_retains_reported_usage(tmp_path):
    import time
    import pytest
    from agent_app.execution import TrackedAdapter, Ledger, ExecutionStop
    from agent_app.adapters.common import ModelResponse, TextBlock
    ledger = Ledger(tmp_path, 2, 1, 100, 10)
    class Adapter:
        def create(self, **kwargs):
            ledger.deadline = time.monotonic()-1
            return ModelResponse([TextBlock('late')], 'end_turn', {'input_tokens': 3, 'output_tokens': 2})
    with pytest.raises(ExecutionStop):
        TrackedAdapter(Adapter(), ledger).create(model='test')
    assert ledger.input_tokens == 3
    assert ledger.output_tokens == 2
    assert ledger.known is True


def test_secret_in_tool_input_key_is_redacted(tmp_path, monkeypatch):
    monkeypatch.setenv('TEST_API_KEY', 'private-marker')
    response = {'content': [{'type': 'tool_use', 'id': '1', 'name': 'unpermitted', 'input': {'private-marker': 'ordinary'}}], 'stop_reason': 'tool_use'}
    result = run(tmp_path, [response])
    assert result['status'] == 'policy_denied'
    raw = (tmp_path/'out/events.jsonl').read_text()
    assert 'private-marker' not in raw
    events = [json.loads(line) for line in raw.splitlines()]
    assert next(e for e in events if e['type'] == 'tool.started')['input'] == {'[REDACTED]': 'ordinary'}


def test_streaming_console_secret_split_across_writes(tmp_path, monkeypatch, capsys):
    from agent_app.execution import ReplayAdapter
    monkeypatch.setenv('TEST_API_KEY', 'private-marker')
    original = ReplayAdapter.create
    def streaming(self, **kwargs):
        print('private-', end='', flush=True)
        print('marker', end='', flush=True)
        return original(self, **kwargs)
    monkeypatch.setattr(ReplayAdapter, 'create', streaming)
    assert run(tmp_path, [text()])['status'] == 'completed'
    captured = capsys.readouterr()
    assert 'private-marker' not in captured.out + captured.err
    assert '[REDACTED]' in captured.err


def test_anthropic_cache_tokens_enforce_budget(tmp_path):
    usage = {'input_tokens': 2, 'output_tokens': 1, 'cache_read_input_tokens': 100, 'cache_creation_input_tokens': 20}
    replay = tmp_path/'r.json'
    replay.write_text(json.dumps([text(usage)]))
    result = execute(task='x', workdir=tmp_path, output=tmp_path/'out', model='test', protocol='anthropic_messages', replay=replay, max_tokens=50)
    assert result['status'] == 'budget_exceeded'
    assert result['usage']['total_tokens'] == 123
    events = [json.loads(line) for line in (tmp_path/'out/events.jsonl').read_text().splitlines()]
    usage = next(e['usage'] for e in events if e['type'] == 'model.completed')
    assert usage['cache_read_tokens'] == 100
    assert usage['cache_write_tokens'] == 20


def test_normalized_usage_not_double_counted(tmp_path):
    from agent_app.execution import TrackedAdapter, Ledger
    from agent_app.core.context_budget import ModelUsage
    from agent_app.adapters.common import ModelResponse, TextBlock
    class Adapter:
        def create(self, **kwargs):
            return ModelResponse([TextBlock('ok')], 'end_turn', ModelUsage(122, 1, 100, 20))
    ledger = Ledger(tmp_path, 2, 1, 200, 10, 'openai_responses')
    TrackedAdapter(Adapter(), ledger).create(model='test')
    assert ledger.input_tokens + ledger.output_tokens == 123
