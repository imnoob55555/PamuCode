"""External deterministic checks against the final workspace."""
import tempfile
import os
import stat
from pathlib import Path

from .process import execute


def checked_path(root, relative):
    root = Path(root).resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError('grader path escapes workspace')
    return path


def validate_check(check):
    kind = check.get('type')
    if kind == 'command':
        argv = check.get('argv')
        if not isinstance(argv, list) or not argv or not all(isinstance(x, str) for x in argv):
            raise ValueError('command grader requires argv strings')
        timeout = check.get('timeout', 30)
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0 < timeout <= 3600:
            raise ValueError('invalid grader timeout')
    elif kind in {'file_exists', 'file_contains', 'file_equals'}:
        path = check.get('path')
        if not isinstance(path, str) or not path or Path(path).is_absolute() or '..' in Path(path).parts:
            raise ValueError('invalid grader path')
        if kind != 'file_exists' and not isinstance(check.get('value'), str):
            raise ValueError('file grader requires value string')
    else:
        raise ValueError(f'unknown grader type: {kind}')


def nofollow_text(root, relative):
    """Read only regular files via directory descriptors; never follow a link."""
    flags = os.O_RDONLY | os.O_NOFOLLOW
    descriptor = os.open(root, flags | os.O_DIRECTORY)
    try:
        parts = Path(relative).parts
        for part in parts[:-1]:
            child = os.open(part, flags | os.O_DIRECTORY, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        try:
            child = os.open(parts[-1], flags | os.O_NONBLOCK, dir_fd=descriptor)
        except FileNotFoundError:
            return None
        with os.fdopen(child, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 128 * 1024 * 1024:
                raise ValueError('grader requires bounded regular files')
            return stream.read().decode(errors='replace')
    except FileNotFoundError:
        return None
    except OSError:
        raise ValueError('grader symlink or non-regular path rejected') from None
    finally:
        os.close(descriptor)


def grade(workspace, checks, *, isolated=False):
    if not checks:
        raise ValueError('at least one external check required')
    rows = []
    for check in checks:
        validate_check(check)
        kind = check['type']
        row = {'type': kind}
        if kind == 'command':
            if isolated:
                raise ValueError('isolated command graders unsupported')
            with tempfile.TemporaryDirectory(prefix='pamu-grader-') as temp:
                result = execute(check['argv'], workspace, temp, check.get('timeout', 30))
                row.update(result)
                row['stdout'] = (Path(temp) / 'stdout.log').read_text(errors='replace')[:10000]
                row['stderr'] = (Path(temp) / 'stderr.log').read_text(errors='replace')[:10000]
            row['passed'] = not result['timed_out'] and result['exit_code'] == 0
        else:
            row['path'] = check['path']
            if isolated:
                text = nofollow_text(workspace, check['path'])
                exists = text is not None
            else:
                path = checked_path(workspace, check['path'])
                exists = path.is_file()
                text = path.read_text(errors='replace') if exists else None
            if kind == 'file_exists':
                row['passed'] = exists
            else:
                row['passed'] = exists and (text == check['value'] if kind == 'file_equals' else check['value'] in text)
        rows.append(row)
    return {'passed': all(row['passed'] for row in rows), 'checks': rows}
