"""Fresh-workspace trials, hard deadlines, resume validation and external grading."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .grading import grade
from .isolation import backend_identity, execute_docker
from .process import execute, cancel_all
from .reporting import rebuild
from .suite import digest, fixture_digest, load_suite, CONFIG_KEYS
from .evidence import hashes as evidence_hashes, seal, validate

ROOT = Path(__file__).resolve().parent.parent


def environment_identity():
    """Fingerprint routing without persisting ambient config or credentials."""
    names = {'PAMU_PROVIDER', 'OPENAI_COMPATIBLE_PROTOCOL', 'PAMU_CONTEXT_WINDOW',
             'MODEL_ID', 'FALLBACK_MODEL_ID'}
    names.update(name for name in os.environ if name.endswith(('_BASE_URL', '_API_BASE')))
    return {name: digest(os.environ[name]) for name in sorted(names) if name in os.environ}


def write_json(path, data):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n')
    temporary.replace(path)


def code_identity():
    paths = [p for directory in ('agent_app', 'pamu_eval') for p in (ROOT / directory).rglob('*.py')]
    paths.extend(ROOT / name for name in ('pyproject.toml', 'uv.lock'))
    result = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT, capture_output=True, text=True)
    return {'commit': result.stdout.strip() if result.returncode == 0 else None,
            'source_digest': digest([(str(p.relative_to(ROOT)), hashlib.sha256(p.read_bytes()).hexdigest())
                                     for p in sorted(paths) if p.is_file()]),
            'python': sys.version}


def _trial(task, repetition, config, directory, command, backend):
    directory.mkdir(parents=True)
    workspace = directory / 'workspace'
    # Recheck immediately before copying, guarding against accidental fixture changes.
    if fixture_digest(task['fixture']) != task['fixture_digest']:
        raise ValueError('fixture identity changed during campaign')
    if backend['name'] == 'local':
        shutil.copytree(task['fixture'], workspace)
    instruction = directory / 'instruction.txt'
    instruction.write_text(task['prompt'])
    argv = [*command, '--task-file', str(instruction), '--output', str(directory / 'agent')]
    for key, value in config.items():
        argv.extend(['--' + key.replace('_', '-'), str(value)])
    if 'replay' in task:
        write_json(directory / 'replay.json', task['replay'])
        argv.extend(['--replay', str(directory / 'replay.json')])
    env = dict(os.environ)
    env['PYTHONPATH'] = str(ROOT)
    # HOME and configuration discovery are controlled by the headless entry, not REPL bootstrap.
    if backend['name'] == 'docker':
        result = execute_docker(task, config, directory, ROOT, backend)
    else:
        result = execute(argv, workspace, directory / 'logs', config['timeout'] + 2, env=env)
    write_json(directory / 'process.json', result)
    row = {'task_id': task['id'], 'repetition': repetition, 'category': task.get('category', 'uncategorized'),
           'duration_seconds': result['duration_seconds'], 'usage': None, 'outcome': 'infra_error',
           'isolation': result.get('isolation', backend)}
    response_path = directory / 'agent' / 'result.json'
    if result['timed_out']:
        row['outcome'] = 'timeout'
    elif response_path.is_file():
        try:
            response = json.loads(response_path.read_text())
            row['usage'] = response.get('usage')
            row['stop_reason'] = response.get('stop_reason')
            row['execution_status'] = response['status']
            if response['status'] == 'completed' and result['exit_code'] == 0:
                grades = grade(workspace, task['checks'], isolated=backend['name'] == 'docker')
                write_json(directory / 'grades.json', grades)
                row['outcome'] = 'passed' if grades['passed'] else 'failed'
            elif response['status'] in {'budget_exceeded', 'policy_denied', 'error', 'cancelled'}:
                row['outcome'] = response['status']
        except (ValueError, KeyError, TypeError, OSError) as exc:
            row['diagnostic'] = type(exc).__name__
    row['evidence'] = evidence_hashes(directory)
    write_json(directory / 'trial.json', row)
    seal(directory)
    return row


def run_suite(suite_path, output, *, repetitions=1, resume=False, config_overrides=None,
              agent_command=None, jobs=1, backend='local', image=None):
    if type(repetitions) is not int or repetitions < 1:
        raise ValueError('repetitions must be positive')
    if type(jobs) is not int or jobs < 1:
        raise ValueError('jobs must be positive')
    suite = load_suite(suite_path)
    config = {**suite['config'], **(config_overrides or {})}
    if set(config) - CONFIG_KEYS:
        raise ValueError('unknown config override')
    backend = backend_identity(backend, image, suite['tasks'], agent_command=agent_command)
    output = Path(output).resolve()
    if any(output.is_relative_to(Path(task['fixture'])) for task in suite['tasks']):
        raise ValueError('output must be outside fixtures')
    output.mkdir(parents=True, exist_ok=True)
    command = agent_command or [sys.executable, '-m', 'agent_app', 'run']
    manifest = {'schema_version': 1, 'suite_fingerprint': suite['fingerprint'],
                'config': config, 'tasks': suite['tasks'], 'repetitions': repetitions,
                'runtime': code_identity(), 'routing_fingerprints': environment_identity(),
                'agent_command': command, 'backend': backend}
    manifest['identity'] = digest(manifest)
    lock = output / '.campaign.lock'
    try:
        handle = lock.open('x')
    except FileExistsError:
        raise ValueError('campaign locked; ensure previous process exited before removing lock') from None
    try:
        handle.write(str(os.getpid()))
        handle.close()
        path = output / 'manifest.json'
        if path.exists():
            previous = json.loads(path.read_text())
            if not resume:
                raise ValueError('output already contains a campaign; use --resume or a new directory')
            if previous != manifest:
                raise ValueError('campaign identity changed; use a new output directory')
        else:
            write_json(path, manifest)
        (output / 'trials').mkdir(exist_ok=True)
        pending = []
        for task in suite['tasks']:
            for repetition in range(1, repetitions + 1):
                directory = output / 'trials' / f'{task["id"]}--{repetition}'
                if directory.exists():
                    row_path = directory / 'trial.json'
                    if not row_path.exists():
                        raise ValueError('incomplete trial retained; use a new output to rerun explicitly')
                    validate(directory)
                    continue
                pending.append((task, repetition, directory))
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            futures = [pool.submit(_trial, task, repetition, config, directory, command, backend)
                       for task, repetition, directory in pending]
            try:
                for future in futures:
                    future.result()
            except BaseException:
                cancel_all()
                for future in futures:
                    future.cancel()
                raise
        report = rebuild(output)
        report['campaign_identity'] = manifest['identity']
        write_json(output / 'report.json', report)
        return report
    finally:
        lock.unlink(missing_ok=True)
