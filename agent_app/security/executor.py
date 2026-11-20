"""The sole model shell launcher, with bounded process group lifetime."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import os
from pathlib import Path
import signal
import selectors
import time
import subprocess
import sys
import tempfile

from .policy import ExecutionPolicy, SandboxUnavailable, child_environment
from ..core.interruption import (
    CommandInterrupted, check_command_cancelled, defer_sigint, protect_interrupt_cleanup,
)


@dataclass(frozen=True, slots=True)
class ShellResult:
    stdout: str
    stderr: str
    exit_code: int
    output_limit_bytes: int = 100000
    output_limit_exceeded: bool = False


class ShellExecutor:
    def __init__(
        self, policy: ExecutionPolicy, *, report: Callable[[str, str, int | None], None] | None = None
    ):
        self.policy = policy
        self.report = report

    @property
    def backend(self) -> str:
        if self.policy.mode == "off":
            return "none"
        return "macos-seatbelt" if sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").is_file() else "unavailable"

    def run(self, command: str, *, cwd: Path | None = None, timeout: float = 120) -> ShellResult:
        check_command_cancelled()
        if not isinstance(command, str):
            raise TypeError("shell command must be a string")
        base = self.policy.check_path(cwd or self.policy.workspace)
        if not base.is_dir():
            raise ValueError("Tool cwd does not exist")
        if self.policy.mode == "confined" and self.backend == "unavailable":
            raise SandboxUnavailable("Confined execution requires the macOS Seatbelt backend; no unsandboxed fallback")
        with tempfile.TemporaryDirectory(prefix="pamu-child-") as temp:
            scratch = Path(temp).resolve()
            (scratch / "home").mkdir(mode=0o700)
            argv = ["/bin/sh", "-c", command]
            if self.policy.mode == "confined":
                from .seatbelt import profile
                sandbox_profile = scratch / "policy.sb"
                sandbox_profile.write_text(profile(self.policy, scratch))
                argv = ["/usr/bin/sandbox-exec", "-f", str(sandbox_profile), *argv]
            proc = None
            interrupted = False
            outcome = 'failed'
            result = None
            group_stopped = False
            output = {"stdout": bytearray(), "stderr": bytearray()}
            deadline = time.monotonic() + max(.001, timeout)

            def kill_group():
                nonlocal group_stopped
                if group_stopped:
                    return
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                group_stopped = True

            with selectors.DefaultSelector() as selector:
                try:
                    check_command_cancelled()
                    with defer_sigint():
                        proc = subprocess.Popen(
                            argv, cwd=base, env=child_environment(os.environ, scratch=scratch),
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=True,
                        )
                        if self.report is not None:
                            self.report('running', command, None)
                    result = self._read_result(proc, selector, output, deadline, timeout, kill_group)
                    if not result.output_limit_exceeded:
                        outcome = 'finished'
                    return result
                except KeyboardInterrupt:
                    if proc is None:
                        raise CommandInterrupted(started=False) from None
                    interrupted = True
                    outcome = 'interrupted'
                except subprocess.TimeoutExpired:
                    outcome = 'timed_out'
                    raise
                finally:
                    if proc is not None:
                        cleanup = protect_interrupt_cleanup if interrupted else defer_sigint
                        with cleanup():
                            kill_group()
                            proc.wait()
                            for name, target in output.items():
                                with getattr(proc, name) as pipe:
                                    if interrupted:
                                        # A detached descendant can keep the pipe open.
                                        os.set_blocking(pipe.fileno(), False)
                                        try:
                                            target.extend(os.read(pipe.fileno(), 100000 - len(target)))
                                        except BlockingIOError:
                                            pass
                            if self.report is not None:
                                self.report(outcome, command, result.exit_code if result is not None else None)
                partial = (output['stdout'] + output['stderr']).decode(errors='replace')
                raise CommandInterrupted(partial)

    def _read_result(self, proc, selector, output, deadline, timeout, kill_group):
        limit_exceeded = False
        for name in output:
            pipe = getattr(proc, name)
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe, selectors.EVENT_READ, name)
        while not limit_exceeded:
            if proc.poll() is None:
                check_command_cancelled(started=True)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(proc.args, timeout)
                delay = min(.05, remaining)
                if not selector.get_map():
                    # Commands may close both streams and keep running.
                    time.sleep(delay)
                    continue
                ready = selector.select(delay)
            else:
                kill_group()
                ready = selector.select(0) if selector.get_map() else []
                if not ready:
                    break
            for key, _ in ready:
                data = os.read(key.fd, 65536)
                if not data:
                    selector.unregister(key.fileobj)
                    continue
                target = output[key.data]
                room = 100000 - len(target)
                target.extend(data[:room])
                limit_exceeded |= len(data) > room

        result = ShellResult(
            output['stdout'].decode(errors='replace'), output['stderr'].decode(errors='replace'),
            125 if limit_exceeded else proc.returncode, output_limit_exceeded=limit_exceeded,
        )
        if self.policy.mode == 'confined' and 'sandbox-exec:' in result.stderr:
            raise SandboxUnavailable('Seatbelt failed to establish confinement; no unsandboxed fallback')
        return result
