"""Bounded subprocess execution with process-group cleanup (POSIX)."""
import os
from pathlib import Path
import signal
import subprocess
import time
import threading

_ACTIVE = set()
_LOCK = threading.Lock()


def cancel_all():
    with _LOCK:
        processes = list(_ACTIVE)
    for process in processes:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def execute(argv, cwd, logs, timeout, *, env=None):
    if timeout <= 0:
        raise ValueError('timeout must be positive')
    logs = Path(logs)
    logs.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    timed_out = False
    with (logs / 'stdout.log').open('wb') as out, (logs / 'stderr.log').open('wb') as err:
        process = subprocess.Popen(argv, cwd=cwd, env=env, stdout=out, stderr=err,
                                   start_new_session=True)
        with _LOCK:
            _ACTIVE.add(process)
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=.5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                pass
        finally:
            # Also reap surviving descendants after an otherwise successful parent.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            with _LOCK:
                _ACTIVE.discard(process)
    return {'exit_code': process.returncode, 'timed_out': timed_out,
            'duration_seconds': time.monotonic() - start}
