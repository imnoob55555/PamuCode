import json
import subprocess
import asyncio
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


def test_swe_patch_includes_new_files_and_excludes_state(tmp_path):
    from pamu_eval.adapters.swebench import capture_patch
    def git(*args):
        return subprocess.run(['git', *args], cwd=tmp_path, check=True, capture_output=True, text=True).stdout
    git('init', '-q')
    (tmp_path / 'existing').write_text('before\n')
    git('add', 'existing')
    git('-c', 'user.name=Eval', '-c', 'user.email=eval@example.invalid', 'commit', '-qm', 'base')
    base = git('rev-parse', 'HEAD').strip()
    (tmp_path / 'existing').write_text('after\n')
    (tmp_path / 'new.py').write_text('value = 1\n')
    (tmp_path / '.pamu').mkdir()
    (tmp_path / '.pamu' / 'state').write_text('private')
    patch = capture_patch(tmp_path, base)
    assert '+after' in patch and '+value = 1' in patch
    assert '.pamu' not in patch
    assert '?? new.py' in git('status', '--short')


def test_swe_export_rejects_missing_and_duplicate_predictions(tmp_path):
    from pamu_eval.adapters.swebench import export_predictions
    output = tmp_path / 'predictions.jsonl'
    row = {'instance_id': 'repo__repo-1', 'model_name_or_path': 'pamu/model', 'model_patch': ''}
    export_predictions([row], output)
    assert json.loads(output.read_text()) == row
    with pytest.raises(ValueError):
        export_predictions([row, row], output)
    with pytest.raises(ValueError):
        export_predictions([{'instance_id': 'missing'}], output)


def test_swe_patch_preserves_crlf(tmp_path):
    from pamu_eval.adapters.swebench import capture_patch
    def git(*args, **kwargs):
        return subprocess.run(['git', *args], cwd=tmp_path, check=True, capture_output=True, **kwargs)
    git('init', '-q')
    (tmp_path / 'a').write_bytes(b'before\r\n')
    git('add', 'a')
    git('-c', 'user.name=Eval', '-c', 'user.email=eval@example.invalid', 'commit', '-qm', 'base')
    base = git('rev-parse', 'HEAD').stdout.decode().strip()
    (tmp_path / 'a').write_bytes(b'after\r\n')
    patch = capture_patch(tmp_path, base)
    assert '+after\r\n' in patch
    (tmp_path / 'a').write_bytes(b'before\r\n')
    git('apply', '-', input=patch.encode())
    assert (tmp_path / 'a').read_bytes() == b'after\r\n'


def test_atif_contains_calls_and_observations():
    from pamu_eval.adapters.trajectory import to_atif
    events = [
        {'type': 'tool.started', 'tool_call_id': 't1', 'name': 'write_file', 'input': {'path': 'a'}},
        {'type': 'tool.completed', 'tool_call_id': 't1', 'output': 'ok'},
    ]
    result = to_atif(events, {'status': 'completed', 'final_answer': 'done', 'usage': {}}, model='model')
    assert result['steps'][0]['tool_calls'][0]['tool_call_id'] == 't1'
    assert result['steps'][0]['observation']['results'][0]['source_call_id'] == 't1'
    assert result['steps'][-1]['message'] == 'done'


def test_atif_refuses_missing_tool_observation():
    from pamu_eval.adapters.trajectory import to_atif
    with pytest.raises(ValueError):
        to_atif([{'type': 'tool.started', 'tool_call_id': 't', 'name': 'bash', 'input': {}}], {'final_answer': ''}, model='m')


def test_harbor_bridge_uses_declared_connection_and_return_code(tmp_path, monkeypatch):
    # Protocol stub tests our adapter contract, not Harbor installation or containers.
    class Base:
        def __init__(self, *args, **kwargs):
            self.logs_dir = tmp_path
            self.model_connection = SimpleNamespace(model_id='selected-model', env={'ANTHROPIC_API_KEY': 'test-only'})
            self.commands = []

        async def exec_as_root(self, environment, **kwargs):
            self.commands.append(kwargs)
            return SimpleNamespace(return_code=0)

        exec_as_agent = exec_as_root

        async def _upload_config_text(self, environment, **kwargs):
            self.instruction = kwargs['content']

    modules = {
        'harbor.agents.installed.base': {'BaseInstalledAgent': Base, 'with_prompt_template': lambda fn: fn},
        'harbor.agents.agent_auth_resolution': {'ModelConnectionSpec': lambda **kw: SimpleNamespace(**kw), 'ProviderAccess': lambda **kw: SimpleNamespace(**kw)},
        'harbor.agents.capabilities': {'AgentCapabilities': lambda **kw: SimpleNamespace(**kw)},
        'harbor.models.model_api_format': {'ModelApiFormat': SimpleNamespace(ANTHROPIC_MESSAGES='anthropic_messages')},
    }
    for name, attrs in modules.items():
        module = ModuleType(name)
        module.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, name, module)
    source = Path(__file__).resolve().parents[1] / 'pamu_eval' / 'adapters' / 'harbor.py'
    spec = importlib.util.spec_from_file_location('harbor_bridge_contract', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    wheel = tmp_path / 'pamu.whl'
    wheel.write_bytes(b'test-wheel')
    agent = module.PamuCodeAgent(wheel_path=wheel)
    assert agent.MODEL_CONNECTION.default_provider == 'anthropic'
    class Environment:
        async def upload_file(self, *args):
            pass
    environment = Environment()
    asyncio.run(agent.install(environment))
    asyncio.run(agent.run('fix this', environment, SimpleNamespace()))
    assert 'selected-model' in agent.commands[-1]['command']
    assert agent.commands[-1]['env']['PAMU_PROVIDER'] == 'anthropic'
