"""Validated task manifests and reproducibility fingerprints."""
import hashlib
import json
import math
import re
import stat
from pathlib import Path

from .grading import validate_check

CONFIG_KEYS = {'model', 'protocol', 'max_model_calls', 'max_tool_calls', 'max_tokens', 'timeout'}
DEFAULT_CONFIG = {'model': 'replay', 'protocol': 'anthropic_messages', 'max_model_calls': 20,
                  'max_tool_calls': 20, 'max_tokens': 100000, 'timeout': 120}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def fixture_digest(root):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError('fixture must be a directory, not a symlink')
    rows = [('.', 'directory', stat.S_IMODE(root.stat().st_mode))]
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise ValueError('fixture symlinks are forbidden')
        if any(part in {'.pamu', '.git', '.settings', '.env', '.venv'} for part in path.relative_to(root).parts):
            raise ValueError('fixture contains private or runtime state')
        mode = stat.S_IMODE(path.stat().st_mode)
        if path.is_file():
            rows.append((str(path.relative_to(root)), 'file', mode, hashlib.sha256(path.read_bytes()).hexdigest()))
        elif path.is_dir():
            rows.append((str(path.relative_to(root)), 'directory', mode))
        else:
            raise ValueError('fixture contains unsupported special file')
    return digest(rows)


def load_suite(path):
    path = Path(path).resolve()
    data = json.loads(path.read_text())
    if data.get('schema_version') != 1 or not isinstance(data.get('tasks'), list) or not data['tasks']:
        raise ValueError('suite requires schema_version=1 and nonempty tasks')
    config = {**DEFAULT_CONFIG, **data.get('config', {})}
    if set(config) - CONFIG_KEYS:
        raise ValueError('unknown execution config key')
    for key in ('max_model_calls', 'max_tool_calls', 'max_tokens'):
        if type(config[key]) is not int or config[key] <= 0:
            raise ValueError(f'{key} must be a positive integer')
    if not isinstance(config['timeout'], (int, float)) or isinstance(config['timeout'], bool) or not math.isfinite(config['timeout']) or config['timeout'] <= 0:
        raise ValueError('timeout must be finite and positive')
    if not all(isinstance(config[k], str) and config[k] for k in ('model', 'protocol')):
        raise ValueError('model and protocol required')
    tasks, seen = [], set()
    for raw in data['tasks']:
        task = dict(raw)
        identifier = task.get('id', '')
        if not isinstance(identifier, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}', identifier):
            raise ValueError('invalid task id')
        if identifier in seen:
            raise ValueError('duplicate task id')
        seen.add(identifier)
        if not isinstance(task.get('prompt'), str) or not task['prompt'].strip():
            raise ValueError('task requires prompt')
        if not isinstance(task.get('checks'), list) or not task['checks']:
            raise ValueError('task requires external checks')
        for check in task['checks']:
            validate_check(check)
        fixture_input = path.parent / task['fixture']
        if fixture_input.is_symlink():
            raise ValueError('fixture symlinks are forbidden')
        fixture = fixture_input.resolve()
        task['fixture_digest'] = fixture_digest(fixture)
        task['fixture'] = str(fixture)
        if 'replay' in task and (not isinstance(task['replay'], list) or not task['replay']):
            raise ValueError('replay requires nonempty response array')
        tasks.append(task)
    canonical = {'schema_version': 1, 'config': config, 'tasks': tasks}
    return {**canonical, 'fingerprint': digest(canonical), 'source': str(path)}
