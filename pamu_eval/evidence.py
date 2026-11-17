"""Integrity receipts for retained evidence (not a hostile-code security boundary)."""
import hashlib
import json
import os
import stat


def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def workspace_hash(directory):
    rows = [('.', 'directory', stat.S_IMODE(directory.stat().st_mode))]
    for path in sorted(directory.rglob('*')):
        relative = str(path.relative_to(directory))
        mode = stat.S_IMODE(path.lstat().st_mode)
        if path.is_symlink():
            rows.append((relative, 'symlink', mode, os.readlink(path)))
        elif path.is_file():
            rows.append((relative, 'file', mode, sha(path)))
        elif path.is_dir():
            rows.append((relative, 'directory', mode))
        else:
            rows.append((relative, 'special'))
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()


def hashes(directory):
    names = ['agent/result.json', 'agent/events.jsonl', 'grades.json', 'process.json']
    result = {name: sha(directory / name) for name in names if (directory / name).is_file()}
    result['workspace'] = workspace_hash(directory / 'workspace')
    return result


def seal(directory):
    receipts = directory.parent.parent / 'receipts'
    receipts.mkdir(exist_ok=True)
    (receipts / (directory.name + '.sha256')).write_text(sha(directory / 'trial.json'))


def validate(directory):
    path = directory / 'trial.json'
    receipt = directory.parent.parent / 'receipts' / (directory.name + '.sha256')
    if not receipt.is_file() or not path.is_file() or receipt.read_text() != sha(path):
        raise ValueError('trial evidence receipt changed or missing')
    row = json.loads(path.read_text())
    if hashes(directory) != row.get('evidence'):
        raise ValueError('trial evidence changed')
    return row
