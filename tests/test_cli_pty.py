import errno
import os
from pathlib import Path
import pty
import select
import subprocess
import sys
import time


def _read_until(master_fd, output, marker, deadline):
    while marker not in output:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(
                f"timed out waiting for {marker!r}; output={bytes(output)!r}"
            )
        readable, _, _ = select.select([master_fd], [], [], remaining)
        if not readable:
            continue
        try:
            chunk = os.read(master_fd, 4096)
        except OSError as exc:
            if exc.errno == errno.EIO:
                break
            raise
        if not chunk:
            break
        output.extend(chunk)
    assert marker in output, bytes(output)


def test_cli_fake_stream_spinner_clears_before_text_and_leaves_no_worker(
    tmp_path,
):
    fake_modules = tmp_path / "fake-modules"
    workspace = tmp_path / "workspace"
    fake_modules.mkdir()
    workspace.mkdir()
    release_read, release_write = os.pipe()
    (fake_modules / "dotenv.py").write_text(
        "def dotenv_values(_path):\n"
        "    return {}\n",
        encoding="utf-8",
    )
    (fake_modules / "anthropic.py").write_text(
        "import os\n"
        "from types import SimpleNamespace\n"
        "class Stream:\n"
        "    def __enter__(self):\n"
        "        return self\n"
        "    def __exit__(self, *args):\n"
        "        return False\n"
        "    @property\n"
        "    def text_stream(self):\n"
        "        os.read(int(os.environ['PAMU_TEST_RELEASE_FD']), 1)\n"
        "        yield 'FAKE_ASSISTANT_TEXT'\n"
        "    def get_final_message(self):\n"
        "        block = SimpleNamespace(type='text', text='FAKE_ASSISTANT_TEXT')\n"
        "        return SimpleNamespace(stop_reason='end_turn', content=[block])\n"
        "class Messages:\n"
        "    def stream(self, **_kwargs):\n"
        "        return Stream()\n"
        "    def create(self, **_kwargs):\n"
        "        return SimpleNamespace(content=[])\n"
        "class Anthropic:\n"
        "    def __init__(self, **_kwargs):\n"
        "        self.messages = Messages()\n",
        encoding="utf-8",
    )
    project_root = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    environment.update(
        {
            "MODEL_ID": "test-model",
            "PAMU_TEST_RELEASE_FD": str(release_read),
            "PYTHONPATH": f"{fake_modules}{os.pathsep}{project_root}",
        }
    )
    command = (
        "from agent_app.cli import main; "
        "main(); "
        "import threading; "
        "print('SPINNER_THREADS=' + str(sum("
        "t.name == 'pamu-working-spinner' for t in threading.enumerate())))"
    )
    master_fd, slave_fd = pty.openpty()
    # One PTY intentionally merges stdout and stderr so cross-stream ordering
    # between the spinner clear and assistant text can be asserted reliably.
    process = subprocess.Popen(
        [sys.executable, "-c", command],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        cwd=workspace,
        env=environment,
        pass_fds=(release_read,),
        close_fds=True,
    )
    os.close(slave_fd)
    os.close(release_read)
    output = bytearray()
    deadline = time.monotonic() + 10.0
    try:
        _read_until(master_fd, output, b">> ", deadline)
        os.write(master_fd, b"hello\n")
        _read_until(master_fd, output, "Working".encode(), deadline)
        os.write(release_write, b"1")
        _read_until(master_fd, output, b"FAKE_ASSISTANT_TEXT", deadline)
        os.write(master_fd, b"q\n")
        _read_until(master_fd, output, b"SPINNER_THREADS=0", deadline)
        process.wait(timeout=max(0.1, deadline - time.monotonic()))
    finally:
        os.close(release_write)
        os.close(master_fd)
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)

    merged = bytes(output)
    spinner_at = merged.index("Working".encode())
    clear_at = merged.index(b"\r\x1b[2K", spinner_at)
    text_at = merged.index(b"FAKE_ASSISTANT_TEXT", clear_at)
    assert spinner_at < clear_at < text_at
    assert process.returncode == 0
