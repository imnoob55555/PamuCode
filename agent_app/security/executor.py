"""The sole model shell launcher, with bounded process group lifetime."""
from __future__ import annotations

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


@dataclass(frozen=True, slots=True)
class ShellResult:
    stdout: str
    stderr: str
    exit_code: int
    output_limit_bytes: int = 100000
    output_limit_exceeded: bool = False


class ShellExecutor:
    def __init__(self, policy: ExecutionPolicy):
        self.policy = policy

    @property
    def backend(self) -> str:
        if self.policy.mode == "off":
            return "none"
        return "macos-seatbelt" if sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").is_file() else "unavailable"

    def run(self, command: str, *, cwd: Path | None = None, timeout: float = 120) -> ShellResult:
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
            proc = subprocess.Popen(argv, cwd=base, env=child_environment(os.environ, scratch=scratch), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
            output = {"stdout": bytearray(), "stderr": bytearray()}
            limit_exceeded = False
            deadline = time.monotonic() + max(.001, timeout)
            selector = selectors.DefaultSelector()
            def kill_group():
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            try:
                for name in output:
                    pipe = getattr(proc, name)
                    os.set_blocking(pipe.fileno(), False)
                    selector.register(pipe, selectors.EVENT_READ, name)
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(argv, timeout)
                    if proc.poll() is not None:
                        # Do not wait for descendants retaining inherited pipes.
                        kill_group()
                    for key, _ in selector.select(min(.05, remaining)):
                        data = os.read(key.fd, 65536)
                        if not data:
                            selector.unregister(key.fileobj)
                            continue
                        target = output[key.data]
                        room = 100000 - len(target)
                        target.extend(data[:room])
                        if len(data) > room:
                            limit_exceeded = True
                            kill_group()
                    if limit_exceeded:
                        break
                remaining = deadline - time.monotonic()
                if remaining <= 0 and proc.poll() is None:
                    raise subprocess.TimeoutExpired(argv, timeout)
                proc.wait(timeout=max(.001, remaining))
                result = ShellResult(output["stdout"].decode(errors="replace"), output["stderr"].decode(errors="replace"), 125 if limit_exceeded else proc.returncode, output_limit_exceeded=limit_exceeded)
                if self.policy.mode == "confined" and "sandbox-exec:" in result.stderr:
                    raise SandboxUnavailable("Seatbelt failed to establish confinement; no unsandboxed fallback")
                return result
            finally:
                # Handles timeouts and cancellation BaseExceptions from signals.
                kill_group()
                proc.wait()
                selector.close()
                proc.stdout.close()
                proc.stderr.close()
