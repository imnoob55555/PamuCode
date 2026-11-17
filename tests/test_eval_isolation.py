"""Docker contract tests; they do not constitute a real Docker integration run."""
import io
import json
import tarfile
from pathlib import Path
import subprocess

import pytest

IMAGE = 'sha256:' + 'a' * 64


def archive(*entries):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w') as tar:
        for name, kind, content in entries:
            member = tarfile.TarInfo(name)
            member.type = kind
            member.size = len(content) if kind == tarfile.REGTYPE else 0
            member.linkname = '/etc/passwd' if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE) else ''
            tar.addfile(member, io.BytesIO(content) if member.size else None)
    return stream.getvalue()


def suite_file(tmp_path, *, replay=True, command=False):
    (tmp_path / 'fixture').mkdir()
    task = {'id': 'task', 'fixture': 'fixture', 'prompt': 'fix',
            'checks': [{'type': 'command', 'argv': ['true']}] if command else
                      [{'type': 'file_equals', 'path': 'answer', 'value': 'correct'}]}
    if replay:
        task['replay'] = [{'content': [{'type': 'text', 'text': 'done'}], 'stop_reason': 'end_turn'}]
    path = tmp_path / 'suite.json'
    path.write_text(json.dumps({'schema_version': 1, 'tasks': [task]}))
    return path


@pytest.mark.parametrize('name,kind', [('../escape', tarfile.REGTYPE), ('/escape', tarfile.REGTYPE),
    ('x', tarfile.SYMTYPE), ('x', tarfile.LNKTYPE), ('x', tarfile.FIFOTYPE), ('x', tarfile.CHRTYPE)])
def test_export_rejects_unsafe_tar(tmp_path, name, kind):
    from pamu_eval.isolation import extract_export
    path = tmp_path / 'export.tar'
    path.write_bytes(archive((name, kind, b'bad')))
    with pytest.raises(ValueError, match='export'):
        extract_export(path, tmp_path / 'destination')
    assert not (tmp_path / 'escape').exists()


def test_export_bounds_files_and_bytes_and_never_follows_existing_links(tmp_path):
    from pamu_eval.isolation import extract_export
    path = tmp_path / 'export.tar'
    path.write_bytes(archive(('answer', tarfile.REGTYPE, b'correct')))
    with pytest.raises(ValueError, match='export'):
        extract_export(path, tmp_path / 'small', max_bytes=3)
    with pytest.raises(ValueError, match='export'):
        extract_export(path, tmp_path / 'few', max_files=0)
    target = tmp_path / 'existing'
    target.mkdir()
    (target / 'answer').symlink_to(tmp_path / 'secret')
    with pytest.raises(ValueError, match='export'):
        extract_export(path, target)
    assert not (tmp_path / 'secret').exists()
    extract_export(path, tmp_path / 'good')
    assert (tmp_path / 'good' / 'answer').read_text() == 'correct'


def test_docker_requires_pin_replay_and_file_grading_before_contacting_daemon(tmp_path, monkeypatch):
    from pamu_eval.runner import run_suite
    def forbidden(*args, **kwargs):
        pytest.fail('must validate before contacting Docker')
    monkeypatch.setattr(subprocess, 'run', forbidden)
    path = suite_file(tmp_path, replay=False)
    with pytest.raises(ValueError, match='pinned'):
        run_suite(path, tmp_path / 'out', backend='docker', image='python:latest')
    with pytest.raises(ValueError, match='replay'):
        run_suite(path, tmp_path / 'out', backend='docker', image=IMAGE)
    data = json.loads(path.read_text())
    data['tasks'][0]['replay'] = [{}]
    data['tasks'][0]['checks'] = [{'type': 'command', 'argv': ['true']}]
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='command grader'):
        run_suite(path, tmp_path / 'out', backend='docker', image=IMAGE)


def test_isolated_file_grader_rejects_symlinks_and_special_files(tmp_path):
    from pamu_eval.grading import grade
    secret = tmp_path / 'secret'
    secret.write_text('private')
    root = tmp_path / 'workspace'
    root.mkdir()
    (root / 'link').symlink_to(secret)
    with pytest.raises(ValueError, match='symlink|regular'):
        grade(root, [{'type': 'file_contains', 'path': 'link', 'value': 'private'}], isolated=True)
    (root / 'sub').symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match='symlink|regular'):
        grade(root, [{'type': 'file_exists', 'path': 'sub/secret'}], isolated=True)


class FakeDocker:
    def __init__(self, *, cleanup_failure=False, running=False):
        self.calls = []
        self.cleanup_failure = cleanup_failure
        self.running = running
        self.created = None

    def install(self, monkeypatch):
        monkeypatch.setattr(subprocess, 'run', self)
        monkeypatch.setattr(subprocess, 'Popen', self.popen)

    def popen(self, argv, **kwargs):
        output = io.BytesIO()
        self(argv, **{**kwargs, 'stdout': output})
        output.seek(0)
        class ExportProcess:
            pid = 99999999
            stdout = output
            def wait(self):
                return 0
        return ExportProcess()

    def __call__(self, argv, **kwargs):
        if argv[0] == 'git':
            return subprocess.CompletedProcess(argv, 0, 'test-commit', '')
        self.calls.append((argv, kwargs))
        assert argv[0] == 'docker'
        if argv[1:3] == ['image', 'inspect']:
            return subprocess.CompletedProcess(argv, 0, json.dumps([{'Id': IMAGE, 'Config': {'Env': ['PATH=/usr/bin']}}]), '')
        if argv[1] == 'create':
            self.created = argv
            return subprocess.CompletedProcess(argv, 0, 'container123\n', '')
        if argv[1] == 'inspect':
            mounts = []
            for i, arg in enumerate(self.created):
                if arg == '--mount':
                    spec = dict(part.split('=', 1) for part in self.created[i + 1].split(',') if '=' in part)
                    mounts.append({'Type': 'bind', 'Source': spec['source'], 'Destination': spec['target'], 'RW': False})
            info = {'State': {'Running': self.running}, 'Image': IMAGE, 'Config': {'User': '65534:65534'},
                'HostConfig': {'NetworkMode': 'none', 'Privileged': False, 'Memory': 536870912,
                'MemorySwap': 536870912, 'NanoCpus': 1000000000, 'PidsLimit': 128, 'CapDrop': ['ALL'],
                'SecurityOpt': ['no-new-privileges:true'], 'StorageOpt': {'size': '512m'}, 'LogConfig': {'Type': 'none'}},
                'Mounts': mounts}
            return subprocess.CompletedProcess(argv, 0, json.dumps([info]), '')
        if argv[1] == 'wait':
            return subprocess.CompletedProcess(argv, 0, '0', '')
        if argv[1] == 'cp':
            content = archive(('workspace', tarfile.DIRTYPE, b''), ('workspace/answer', tarfile.REGTYPE, b'correct'),
                ('agent', tarfile.DIRTYPE, b''), ('agent/result.json', tarfile.REGTYPE, b'{"status":"completed","usage":null}'),
                ('agent/events.jsonl', tarfile.REGTYPE, b''))
            kwargs['stdout'].write(content)
            return subprocess.CompletedProcess(argv, 0, None, '')
        if argv[1] == 'rm' and self.cleanup_failure:
            return subprocess.CompletedProcess(argv, 1, '', 'cannot remove')
        return subprocess.CompletedProcess(argv, 0, '', '')


def test_docker_fake_end_to_end_contract_manifest_and_resume(tmp_path, monkeypatch):
    from pamu_eval.runner import run_suite
    docker = FakeDocker()
    docker.install(monkeypatch)
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'do-not-forward')
    monkeypatch.setenv('DOCKER_HOST', 'tcp://untrusted.invalid')
    path = suite_file(tmp_path)
    output = tmp_path / 'campaign'
    result = run_suite(path, output, backend='docker', image=IMAGE)
    assert result['passed'] == 1
    manifest = json.loads((output / 'manifest.json').read_text())
    assert manifest['backend']['name'] == 'docker'
    assert manifest['backend']['image_id'] == IMAGE
    assert manifest['backend']['network'] == 'none'
    assert manifest['backend']['provider_credentials'] == 'absent'
    row = json.loads((output / 'trials/task--1/trial.json').read_text())
    assert row['isolation']['container_stopped_before_export'] is True
    assert row['isolation']['cleanup_confirmed'] is True
    create = docker.created
    for flag in ('--network=none', '--cap-drop=ALL', '--security-opt=no-new-privileges:true',
                 '--memory=512m', '--memory-swap=512m', '--cpus=1', '--pids-limit=128', '--user=65534:65534',
                 '--storage-opt=size=512m'):
        assert flag in create
    assert 'PAMU_SANDBOX=off' in create
    assert not any('docker.sock' in arg or str(Path.home()) == arg for arg in create)
    assert all('readonly' in create[i + 1] for i, arg in enumerate(create) if arg == '--mount')
    assert 'do-not-forward' not in repr(docker.calls)
    assert all('DOCKER_HOST' not in call[1]['env'] for call in docker.calls)
    actions = [call[0][1] for call in docker.calls]
    assert actions.index('stop') < actions.index('cp') < actions.index('rm')
    cp = next(call[0] for call in docker.calls if call[0][1] == 'cp')
    assert cp[2].endswith(':/tmp/pamu-trial/.')
    assert '--attach' not in repr(docker.calls)
    assert run_suite(path, output, backend='docker', image=IMAGE, resume=True)['passed'] == 1
    with pytest.raises(ValueError, match='identity'):
        run_suite(path, output, backend='local', resume=True)


@pytest.mark.parametrize('mode', ['running', 'cleanup_failure'])
def test_docker_never_grades_or_seals_on_failed_stop_or_cleanup(tmp_path, monkeypatch, mode):
    from pamu_eval.runner import run_suite
    docker = FakeDocker(**{mode: True})
    docker.install(monkeypatch)
    output = tmp_path / 'campaign'
    with pytest.raises(ValueError, match='stopped|cleanup'):
        run_suite(suite_file(tmp_path), output, backend='docker', image=IMAGE)
    assert not (output / 'trials/task--1/grades.json').exists()
    assert not (output / 'trials/task--1/trial.json').exists()
    assert not (output / 'receipts/task--1.sha256').exists()


def test_no_docker_binary_never_falls_back_to_local(tmp_path, monkeypatch):
    from pamu_eval.runner import run_suite
    def absent(*args, **kwargs):
        raise FileNotFoundError('docker')
    monkeypatch.setattr(subprocess, 'run', absent)
    with pytest.raises(ValueError, match='Docker'):
        run_suite(suite_file(tmp_path), tmp_path / 'output', backend='docker', image=IMAGE)


def test_docker_refuses_image_environment_and_implicit_volumes(tmp_path, monkeypatch):
    from pamu_eval.isolation import backend_identity
    for config in ({'Env': ['ANTHROPIC_API_KEY=embedded']}, {'Volumes': {'/data': {}}}):
        monkeypatch.setattr(subprocess, 'run', lambda *a, **k: subprocess.CompletedProcess(
            a[0], 0, json.dumps([{'Id': IMAGE, 'Config': config}]), ''))
        with pytest.raises(ValueError, match='environment|volumes'):
            backend_identity('docker', IMAGE, [{'replay': [{}], 'checks': []}])


def test_docker_checks_daemon_enforcement_before_start(tmp_path, monkeypatch):
    from pamu_eval.runner import run_suite
    docker = FakeDocker()
    original = docker.__call__
    def altered(argv, **kwargs):
        result = original(argv, **kwargs)
        if argv[1] == 'inspect' and 'start' not in [call[0][1] for call in docker.calls]:
            info = json.loads(result.stdout)
            info[0]['HostConfig']['NetworkMode'] = 'host'
            return subprocess.CompletedProcess(argv, 0, json.dumps(info), '')
        return result
    docker.install(monkeypatch)
    monkeypatch.setattr(subprocess, 'run', altered)
    with pytest.raises(ValueError, match='enforcement'):
        run_suite(suite_file(tmp_path), tmp_path / 'campaign', backend='docker', image=IMAGE)
    assert 'start' not in [call[0][1] for call in docker.calls]
    assert docker.calls[-1][0][1] == 'rm'


def test_docker_timeout_is_recorded_after_stopping_and_cleanup(tmp_path, monkeypatch):
    from pamu_eval.runner import run_suite
    docker = FakeDocker()
    original = docker.__call__
    def timeout(argv, **kwargs):
        if argv[1] == 'wait':
            raise subprocess.TimeoutExpired(argv, .01)
        return original(argv, **kwargs)
    docker.install(monkeypatch)
    monkeypatch.setattr(subprocess, 'run', timeout)
    output = tmp_path / 'campaign'
    report = run_suite(suite_file(tmp_path), output, backend='docker', image=IMAGE)
    assert report['passed'] == 0
    row = json.loads((output / 'trials/task--1/trial.json').read_text())
    assert row['outcome'] == 'timeout'
    assert row['isolation']['cleanup_confirmed']
    assert not (output / 'trials/task--1/grades.json').exists()


def test_docker_cli_exposes_backend_and_pin(tmp_path, monkeypatch):
    from pamu_eval import __main__ as cli
    captured = {}
    def run(*args, **kwargs):
        captured.update(kwargs)
        return {'total': 1, 'passed': 1}
    monkeypatch.setattr(cli, 'run_suite', run)
    assert cli.main(['run', 'suite.json', '--output', str(tmp_path), '--backend', 'docker', '--image', IMAGE]) == 0
    assert captured['backend'] == 'docker' and captured['image'] == IMAGE


def test_docker_client_searches_standard_install_paths_without_ambient_path(monkeypatch):
    from pamu_eval.isolation import docker_environment
    monkeypatch.setenv('PATH', '/attacker/bin')
    paths = docker_environment()['PATH'].split(':')
    assert '/usr/local/bin' in paths and '/opt/homebrew/bin' in paths
    assert '/attacker/bin' not in paths and '' not in paths and '.' not in paths
