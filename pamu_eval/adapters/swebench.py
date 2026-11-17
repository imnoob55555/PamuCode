"""SWE-bench patch capture and official prediction JSONL export."""
import json
import os
import subprocess
import tempfile
from pathlib import Path

EXCLUDES = [':(exclude).pamu', ':(exclude).env', ':(exclude).settings',
            ':(exclude).venv', ':(exclude,glob)**/__pycache__/**',
            ':(exclude,glob)**/.env', ':(exclude,glob)**/.settings']


def capture_patch(workspace, base_commit):
    """Use a disposable index, preserving the subject's index, HEAD and files."""
    def git(*args, env=None):
        return subprocess.run(['git', *args], cwd=workspace, env=env, check=True,
                              capture_output=True, timeout=60).stdout.decode('utf-8')
    base = git('rev-parse', '--verify', base_commit + '^{commit}').strip()
    with tempfile.TemporaryDirectory(prefix='pamu-index-') as temp:
        env = {**os.environ, 'GIT_INDEX_FILE': str(Path(temp) / 'index')}
        git('read-tree', base, env=env)
        git('add', '-A', '--', '.', *EXCLUDES, env=env)
        return git('diff', '--cached', '--binary', base, '--', '.', *EXCLUDES, env=env)


def export_predictions(rows, output):
    seen, validated = set(), []
    for row in rows:
        keys = ('instance_id', 'model_name_or_path', 'model_patch')
        if any(not isinstance(row.get(key), str) for key in keys):
            raise ValueError('prediction requires instance_id, model_name_or_path and model_patch strings')
        if not row['instance_id'] or not row['model_name_or_path'] or row['instance_id'] in seen:
            raise ValueError('empty or duplicate prediction identity')
        seen.add(row['instance_id'])
        validated.append({key: row[key] for key in keys})
    Path(output).write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in validated))
    return len(validated)
