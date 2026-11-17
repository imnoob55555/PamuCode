"""Fixed offline entry point inside the disposable Docker trial container."""
import json
import os
from pathlib import Path
import shutil


def main():
    # /tmp is writable on the trusted clean image; the UID owns only its trial.
    root = Path('/tmp/pamu-trial')
    root.mkdir(mode=0o700)
    workspace = root / 'workspace'
    shutil.copytree('/inputs/fixture', workspace)
    for path in [workspace, *workspace.rglob('*')]:
        path.chmod(0o700 if path.is_dir() else 0o600 | (path.stat().st_mode & 0o100))
    os.chdir(workspace)
    config = json.loads(Path('/inputs/config.json').read_text())
    argv = ['run', '--task-file', '/inputs/instruction.txt', '--output', str(root / 'agent'),
            '--replay', '/inputs/replay.json']
    for key, value in config.items():
        argv.extend(['--' + key.replace('_', '-'), str(value)])
    from agent_app.cli import main as cli_main
    return cli_main(argv)


if __name__ == '__main__':
    raise SystemExit(main())
