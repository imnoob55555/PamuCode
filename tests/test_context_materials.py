from types import SimpleNamespace
import threading
from agent_app.runtime import SessionState, ModelRouter
from agent_app.core.context import build_context
from agent_app.core.prompt import assemble_system_prompt
from agent_app.features.skills import SkillState
from agent_app.features.memory import MemoryStore


def rt(path):
    return SimpleNamespace(config=SimpleNamespace(workdir=path, primary_model='test', default_max_tokens=1024, context_window=4096, context_window_target=None),
        model=ModelRouter(primary_model='test'), session=SessionState(), skills=SkillState(path/'skills'),
        memory=MemoryStore(path/'memory', path/'memory'/'MEMORY.md'),
        mcp=SimpleNamespace(lock=threading.Lock(), clients={}), team=SimpleNamespace(lock=threading.Lock(), active={}))


def test_successful_file_tracking_limits_and_staleness(tmp_path):
    from agent_app.core.context_materials import remember_file
    runtime = rt(tmp_path)
    for i in range(7):
        path = tmp_path/f'{i}.py'
        path.write_text('hello world\n' * 200)
        remember_file(runtime.session, tmp_path, str(path), offset=1, limit=3)
    records = runtime.session.context_state['recent_files']
    assert len(records) == 5 and records[-1]['path'] == '6.py'
    (tmp_path/'6.py').write_text('changed')
    context = build_context(runtime, [])
    assert '已变化' in context['recent_files']
    assert context['recent_files'].count('片段：') <= 2
    assert 'hello world' in context['recent_files']


def test_optional_materials_bounded_and_required_rules_intact(tmp_path):
    from agent_app.core.context_materials import text_tokens
    runtime = rt(tmp_path)
    runtime.memory.root.mkdir()
    runtime.memory.index_path.write_text('memory detail\n' * 10000)
    (tmp_path/'AGENTS.md').write_text('REQUIRED' * 10000)
    runtime.session.context_state['pins'] = [{'text': 'KEEP EXACT'}]
    context = build_context(runtime, [])
    total = sum(text_tokens(context[key]) for key in ('memories', 'skills', 'loaded_skills', 'recent_files'))
    assert total <= context['optional_budget_tokens']
    assert context['project_rules'] == 'REQUIRED' * 10000
    assert 'KEEP EXACT' in assemble_system_prompt(context)


def test_secret_symlink_and_outside_files_never_rehydrated(tmp_path):
    from agent_app.core.context_materials import remember_file
    runtime = rt(tmp_path)
    for name in ('.env', '.settings', 'credentials.json', 'normal.txt'):
        (tmp_path/name).write_text('SECRET')
    (tmp_path/'link.txt').symlink_to(tmp_path/'normal.txt')
    for name in ('.env', '.settings', 'credentials.json', 'link.txt', '../outside'):
        remember_file(runtime.session, tmp_path, name)
    assert runtime.session.context_state.get('recent_files', []) == []


def test_loaded_skill_rereads_current_registry_path(tmp_path):
    from agent_app.core.context_materials import remember_skill
    runtime = rt(tmp_path)
    directory = runtime.skills.root/'example'
    directory.mkdir(parents=True)
    (directory/'SKILL.md').write_text('old skill')
    runtime.skills.registry['example'] = {'name': 'example', 'description': 'test', 'content': 'old skill'}
    remember_skill(runtime.session, 'example', 'old skill')
    (directory/'SKILL.md').write_text('new skill')
    context = build_context(runtime, [])
    assert 'new skill' in context['loaded_skills'] and '已变化' in context['loaded_skills']


def test_runtime_records_only_successful_tools(tmp_path):
    from agent_app.bootstrap import build_runtime
    from agent_app.config import AppConfig
    from tests.test_agent_app_bootstrap import FakeSDKClient
    config = AppConfig.from_env(tmp_path, environ={'MODEL_ID': 'test'})
    skill = config.skills_dir/'demo'/'SKILL.md'
    skill.parent.mkdir(parents=True)
    skill.write_text('---\nname: demo\ndescription: example\n---\nSkill instructions')
    runtime = build_runtime(config, FakeSDKClient())
    _, handlers = runtime.tools.snapshot()
    assert handlers['read_file']('missing').startswith('Error:')
    assert runtime.session.context_state.get('recent_files', []) == []
    handlers['write_file']('example.py', 'line1\nline2\n')
    handlers['read_file']('example.py', offset=1, limit=1)
    record = runtime.session.context_state['recent_files'][0]
    assert record['path'] == 'example.py' and record['offset'] == 1 and record['limit'] == 1
    handlers['load_skill']('missing')
    assert runtime.session.context_state.get('loaded_skills', []) == []
    handlers['load_skill']('demo')
    assert runtime.session.context_state['loaded_skills'][0]['name'] == 'demo'
    runtime.sessions.close()


def test_memory_selector_and_injection_are_bounded(tmp_path):
    from agent_app.features.memory import build_request_messages_with_memories
    from agent_app.core.context_materials import text_tokens
    store = MemoryStore(tmp_path, tmp_path/'MEMORY.md')
    for i in range(70):
        (tmp_path/f'{i:02}.md').write_text('---\nname: note\ndescription: ' + 'x'*10000 + '\n---\n' + 'BODY'*10000)
    prompts = []
    def summarize(prompt, tokens):
        prompts.append(prompt)
        return '[0, 1, 2]'
    messages = [{'role': 'user', 'content': 'question'}]
    output = build_request_messages_with_memories(store, messages, summarize, max_tokens=120)
    suffix = output[0]['content'][len('question\n\n'):]
    assert text_tokens(suffix) <= 120
    assert len(prompts[0]) < 7000
    assert messages == [{'role': 'user', 'content': 'question'}]
