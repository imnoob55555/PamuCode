import os

from agent_app.security import ExecutionPolicy
from agent_app.tools import builtin


def test_confined_read_rejects_fifo_and_hardlink(tmp_path):
    root = tmp_path / 'workspace'
    root.mkdir()
    policy = ExecutionPolicy(root, tmp_path / 'install', mode='confined')
    os.mkfifo(root / 'pipe')
    assert builtin.run_read(root, 'pipe', policy=policy).startswith('Error:')
    outside = tmp_path / 'secret'
    outside.write_text('fake-secret')
    os.link(outside, root / 'alias')
    assert builtin.run_read(root, 'alias', policy=policy).startswith('Error:')
    assert builtin.run_write(root, 'alias', 'changed', policy=policy).startswith('Error:')
    assert outside.read_text() == 'fake-secret'


def test_confined_check_open_race_does_not_follow_symlink(tmp_path, monkeypatch):
    root = tmp_path / 'workspace'
    root.mkdir()
    (root / 'folder').mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'file').write_text('fake-secret')
    policy = ExecutionPolicy(root, tmp_path / 'install', mode='confined')
    original = builtin.safe_path
    def swap(*args, **kwargs):
        checked = original(*args, **kwargs)
        (root / 'folder').rename(root / 'previous')
        (root / 'folder').symlink_to(outside)
        return checked
    monkeypatch.setattr(builtin, 'safe_path', swap)
    assert builtin.run_write(root, 'folder/file', 'changed', policy=policy).startswith('Error:')
    assert (outside / 'file').read_text() == 'fake-secret'


def test_confined_files_support_nested_create_and_edit(tmp_path):
    policy = ExecutionPolicy(tmp_path, tmp_path / 'install', mode='confined')
    assert builtin.run_write(tmp_path, 'a/b/file', 'before', policy=policy).startswith('Wrote')
    assert builtin.run_edit(tmp_path, 'a/b/file', 'before', 'after', policy=policy).startswith('Edited')
    assert builtin.run_read(tmp_path, 'a/b/file', policy=policy) == 'after'


def test_confined_context_does_not_reopen_cached_files(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from agent_app.core.context_materials import optional_materials
    runtime = SimpleNamespace(config=SimpleNamespace(sandbox_mode='confined'))
    result = optional_materials(runtime, 100)
    assert result['recent_files'] == ''
    assert result['optional_remaining_tokens'] == 100


def test_confined_project_rules_reject_link(tmp_path):
    import pytest
    from agent_app.core.context import _project_rules
    marker = tmp_path / 'marker'
    marker.write_text('fake-secret')
    (tmp_path / 'AGENTS.md').symlink_to(marker)
    with pytest.raises(OSError):
        _project_rules(tmp_path, confined=True)


def test_confined_policy_rejects_case_aliases(tmp_path):
    import pytest
    from agent_app.security import SecurityDenied
    policy = ExecutionPolicy(tmp_path, tmp_path, mode='confined')
    for name in ('.PAMU/file', '.ENV', '.EnV.local', '.SsH/file', 'AGENT_APP/file.py', 'PyProject.toml', '.GiT/HOOKS/file'):
        with pytest.raises(SecurityDenied):
            policy.check_path(tmp_path / name, write=True)
    with pytest.raises(SecurityDenied):
        policy.check_path(tmp_path / '.PAMU/file')
