"""Opt-in replay-only Docker trials. The daemon/image are trusted infrastructure.

No live provider broker is implemented. Images must already exist locally, contain
Python 3.13+ and PamuCode's dependencies, and have no embedded secrets. The Docker
storage driver MUST support writable-layer size quotas; unsupported drivers fail
closed at create. Source/input snapshots are read-only; canonical evidence is
never mounted. Container output is untrusted data, not authenticated telemetry.
"""
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
import uuid

MAX_EXPORT_BYTES = 128 * 1024 * 1024
MAX_EXPORT_FILES = 10000
_PIN = re.compile(r'(?:[a-zA-Z0-9][a-zA-Z0-9._:/-]*@)?sha256:[0-9a-f]{64}')
_ENV_NAMES = {'PATH', 'LANG', 'LC_ALL', 'PYTHON_VERSION', 'PYTHON_SHA256', 'PYTHON_PIP_VERSION',
              'PYTHON_SETUPTOOLS_VERSION', 'GPG_KEY'}


def docker_environment():
    # No DOCKER_HOST/context/proxy/credentials inherited from the campaign shell.
    return {'PATH': '/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin', 'HOME': '/nonexistent', 'DOCKER_CONFIG': '/nonexistent',
            'LANG': 'C.UTF-8'}


def _run(args, *, timeout=30):
    try:
        result = subprocess.run(['docker', *args], env=docker_environment(), capture_output=True,
                                text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f'Docker unavailable or operation timed out: {type(exc).__name__}') from None
    if result.returncode:
        # Do not reflect arbitrary image/daemon output into evidence or error logs.
        raise ValueError(f'Docker {args[0]} failed (exit {result.returncode})')
    return result


def backend_identity(name, image, tasks, *, agent_command=None):
    if name == 'local':
        if image is not None:
            raise ValueError('--image requires Docker backend')
        return {'name': 'local', 'mode': 'trusted-suite', 'isolation': 'none'}
    if name != 'docker':
        raise ValueError('unknown backend')
    if not isinstance(image, str) or not _PIN.fullmatch(image):
        raise ValueError('Docker image must use a pinned sha256 identifier')
    if agent_command is not None:
        raise ValueError('Docker does not accept custom host agent commands')
    if any('replay' not in task for task in tasks):
        raise ValueError('Docker backend requires replay for every task; live credentials unsupported')
    if any(check['type'] == 'command' for task in tasks for check in task['checks']):
        raise ValueError('Docker command graders are unsupported; use file checks')
    info = json.loads(_run(['image', 'inspect', image]).stdout)[0]
    config = info.get('Config') or {}
    if config.get('Volumes'):
        raise ValueError('Docker image declares unbounded volumes')
    for item in config.get('Env') or []:
        if item.partition('=')[0] not in _ENV_NAMES:
            raise ValueError('Docker image contains unsupported environment; use a clean replay image')
    image_id = info.get('Id')
    if not isinstance(image_id, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', image_id):
        raise ValueError('Docker image identity missing')
    if image.startswith('sha256:') and image != image_id:
        raise ValueError('Docker image identity mismatch')
    return {'name': 'docker', 'mode': 'replay-only', 'image': image, 'image_id': image_id,
            'network': 'none', 'provider_credentials': 'absent', 'capabilities': 'none',
            'no_new_privileges': True, 'user': '65534:65534', 'memory_bytes': 536870912,
            'cpu_limit': 1, 'pids_limit': 128, 'writable_layer_bytes': 536870912,
            'storage_quota_required': True, 'host_mounts': 'readonly-source-and-inputs-only',
            'grader': 'host-no-follow-file-checks-after-stop', 'runtime_sandbox': 'off-outer-container',
            'evidence': 'controller-receipts-not-adversarial-score-signatures'}


def extract_export(archive, destination, *, max_bytes=MAX_EXPORT_BYTES, max_files=MAX_EXPORT_FILES):
    """Extract uncompressed bounded tar data into a new tree; never call extractall."""
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise ValueError('export destination must be new')
    if Path(archive).stat().st_size > max_bytes + 16 * 1024 * 1024:
        raise ValueError('export archive too large')
    total = count = 0
    seen = set()
    with tarfile.open(archive, mode='r:') as tar:
        # Validate all headers before writing. Reject sparse/PAX/link/device tricks.
        members = []
        for member in tar:
            count += 1
            path = PurePosixPath(member.name)
            if (count > max_files or path.is_absolute() or '..' in path.parts or
                    '\\' in member.name or not path.parts or len(member.name) > 4096 or
                    member.name in seen or not (member.isfile() or member.isdir()) or
                    member.issparse() or member.size < 0):
                raise ValueError('unsafe export entry')
            total += member.size
            if total > max_bytes:
                raise ValueError('export exceeds byte limit')
            seen.add(member.name)
            members.append((member, path))
        destination.mkdir(mode=0o700, parents=True)
        for member, relative in members:
            target = destination.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            if member.isdir():
                target.mkdir(exist_ok=True)
            else:
                source = tar.extractfile(member)
                with source, target.open('xb') as stream:
                    remaining = member.size
                    while remaining:
                        chunk = source.read(min(65536, remaining))
                        if not chunk:
                            raise ValueError('truncated export file')
                        stream.write(chunk)
                        remaining -= len(chunk)
                target.chmod(0o600 | (member.mode & 0o100))


def _copy_export(name, archive):
    """Bound data while streaming, including sparse-file expansion and stalled cp."""
    process = subprocess.Popen(['docker', 'cp', f'{name}:/tmp/pamu-trial/.', '-'], env=docker_environment(),
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
    def kill():
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    timer = threading.Timer(30, kill)
    timer.start()
    try:
        count = 0
        with Path(archive).open('xb') as target:
            while chunk := process.stdout.read(65536):
                count += len(chunk)
                if count > MAX_EXPORT_BYTES + 16 * 1024 * 1024:
                    raise ValueError('export archive exceeds byte limit')
                target.write(chunk)
        if process.wait() != 0:
            raise ValueError('Docker export failed')
    finally:
        timer.cancel()
        kill()
        process.wait()
        process.stdout.close()


def _snapshot(root, source):
    # Only Python source is mounted; no .git, home, .env, .settings, caches, tests or checks.
    for package in ('agent_app', 'pamu_eval'):
        for path in (root / package).rglob('*.py'):
            if path.is_symlink() or not stat.S_ISREG(path.lstat().st_mode):
                raise ValueError('source snapshot must contain regular files')
            target = source / path.relative_to(root)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
            target.chmod(0o444)
    for directory in sorted(source.rglob('*'), reverse=True):
        if directory.is_dir():
            directory.chmod(0o555)
    source.chmod(0o555)


def _verify_container(name, identity, source, inputs):
    """Check daemon's effective create contract before any untrusted code starts."""
    info = json.loads(_run(['inspect', name]).stdout)[0]
    host = info.get('HostConfig', {})
    config = info.get('Config', {})
    expected = {'NetworkMode': 'none', 'Privileged': False, 'Memory': 536870912,
                'MemorySwap': 536870912, 'NanoCpus': 1000000000, 'PidsLimit': 128}
    valid = all(host.get(key) == value for key, value in expected.items())
    valid = valid and host.get('CapDrop') == ['ALL'] and not host.get('CapAdd')
    valid = valid and 'no-new-privileges:true' in (host.get('SecurityOpt') or [])
    valid = valid and host.get('StorageOpt', {}).get('size', '').lower() == '512m'
    valid = valid and (host.get('LogConfig') or {}).get('Type') == 'none'
    valid = valid and not any(host.get(key) for key in ('Devices', 'DeviceRequests', 'Binds', 'VolumesFrom'))
    valid = valid and config.get('User') == '65534:65534' and info.get('Image') == identity['image_id']
    mounts = info.get('Mounts', [])
    expected_mounts = {'/opt/pamu': str(source), '/inputs': str(inputs)}
    valid = valid and len(mounts) == 2 and all(
        mount.get('Type') == 'bind' and mount.get('RW') is False and
        expected_mounts.get(mount.get('Destination')) == mount.get('Source') for mount in mounts)
    if not valid:
        raise ValueError('Docker enforcement contract mismatch; refusing to start')


def execute_docker(task, config, directory, root, identity):
    """Run, stop, export and remove; any cleanup failure prevents host grading."""
    started = time.monotonic()
    name = 'pamu-eval-' + uuid.uuid4().hex
    created = False
    result = {'exit_code': None, 'timed_out': False}
    # This directory is outside canonical artifacts and never mounted writable.
    with tempfile.TemporaryDirectory(prefix='pamu-eval-input-') as temp:
        temp = Path(temp)
        # Parent must be traversable by the container's unprivileged user.
        temp.chmod(0o755)
        source = temp / 'source'
        source.mkdir()
        _snapshot(root, source)
        inputs = temp / 'inputs'
        inputs.mkdir()
        shutil.copytree(task['fixture'], inputs / 'fixture')
        (inputs / 'instruction.txt').write_text(task['prompt'])
        (inputs / 'replay.json').write_text(json.dumps(task['replay']))
        (inputs / 'config.json').write_text(json.dumps(config))
        for path in inputs.rglob('*'):
            path.chmod(0o555 if path.is_dir() else 0o444 | (path.stat().st_mode & 0o100))
        inputs.chmod(0o555)
        argv = ['create', '--name', name, '--network=none', '--cap-drop=ALL',
                '--security-opt=no-new-privileges:true', '--memory=512m', '--memory-swap=512m',
                '--cpus=1', '--pids-limit=128', '--user=65534:65534', '--storage-opt=size=512m',
                '--ulimit=nofile=256:256', '--ulimit=fsize=134217728:134217728', '--ulimit=core=0:0',
                '--log-driver=none', '--workdir=/tmp', '--env', 'HOME=/tmp/pamu-home',
                '--env', 'PAMU_SANDBOX=off', '--env', 'PYTHONDONTWRITEBYTECODE=1',
                '--mount', f'type=bind,source={source},target=/opt/pamu,readonly',
                '--mount', f'type=bind,source={inputs},target=/inputs,readonly',
                '--entrypoint=python3', identity['image_id'], '-c',
                "import sys,runpy;sys.path.insert(0,'/opt/pamu');runpy.run_module('pamu_eval.container_entry',run_name='__main__')"]
        try:
            # A named container can exist even if the client times out during create.
            created = True
            _run(argv)
            _verify_container(name, identity, source, inputs)
            try:
                _run(['start', name])
                waited = _run(['wait', name], timeout=config['timeout'] + 2)
                result['exit_code'] = int(waited.stdout.strip())
            except ValueError as exc:
                if 'timed out' not in str(exc):
                    raise
                result['timed_out'] = True
            _run(['stop', '--time=1', name])
            state = json.loads(_run(['inspect', name]).stdout)[0]['State']
            if state.get('Running') is not False:
                raise ValueError('Docker container not confirmed stopped')
            archive = temp / 'export.tar'
            _copy_export(name, archive)
            exported = temp / 'exported'
            extract_export(archive, exported)
            if any(p.name not in {'workspace', 'agent'} for p in exported.iterdir()):
                raise ValueError('unexpected export root')
            for item in ('workspace', 'agent'):
                source_path = exported / item
                if not source_path.is_dir():
                    raise ValueError('missing export directory')
                shutil.copytree(source_path, directory / item)
            result['isolation'] = {**identity, 'container_stopped_before_export': True,
                                   'export_validated': True, 'cleanup_confirmed': False,
                                   'daemon_contract_verified_before_start': True}
        finally:
            if created:
                try:
                    _run(['rm', '--force', name])
                except ValueError:
                    raise ValueError(f'Docker cleanup failed; inspect/remove container {name} before continuing') from None
        result['isolation']['cleanup_confirmed'] = True
    result['duration_seconds'] = time.monotonic() - started
    return result
