import errno
import os
from pathlib import Path
import pty
import select
import subprocess
import sys
import time
import json
import fcntl
import struct
import termios


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
            "ANTHROPIC_API_KEY": "fake-pty-key",
            "PAMU_TEST_CREDENTIALS_FILE": str(tmp_path / "missing-credentials.json"),
            "PAMU_TEST_RELEASE_FD": str(release_read),
            "PYTHONPATH": f"{fake_modules}{os.pathsep}{project_root}",
        }
    )
    command = (
        "from agent_app.cli import main; "
        "from pathlib import Path; import os; "
        "main(credentials_path=Path(os.environ['PAMU_TEST_CREDENTIALS_FILE'])); "
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


def test_first_launch_login_key_model_and_slash_hints_in_real_terminal(tmp_path):
    project_root = Path(__file__).resolve().parents[1]
    credentials = tmp_path / "credentials.json"
    script = '''
import sys
from pathlib import Path
from types import SimpleNamespace
from agent_app import bootstrap
from agent_app.cli import main
class Messages:
    def create(self, **kw):
        raise AssertionError("onboarding must not verify remotely")
class Client:
    def __init__(self, **kw):
        self.messages = Messages()
sys.modules["anthropic"] = SimpleNamespace(Anthropic=Client)
bootstrap.load_runtime_environment = lambda: {}
main(argv=[], credentials_path=Path(sys.argv[1]))
'''
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(project_root)
    environment["TERM"] = "xterm-256color"
    environment["PROMPT_TOOLKIT_NO_CPR"] = "1"
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 110, 0, 0))
    initial_flags = termios.tcgetattr(slave)
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(credentials)],
        stdin=slave, stdout=slave, stderr=slave, cwd=tmp_path, env=environment,
    )
    output = bytearray()
    deadline = time.monotonic() + 15
    def wait(marker):
        _read_until(master, output, marker.encode(), deadline)
    try:
        wait("请输入 /login")
        wait(">> ")
        assert "选择 Provider".encode() not in output
        os.write(master, b"/")
        wait("配置 API Key 并选择模型")
        wait("显示命令帮助")
        # /login is already highlighted: Enter must execute it immediately.
        os.write(master, b"\r")
        wait("选择 Provider")
        os.write(master, b"\x1b[B\r")
        wait("DeepSeek · 输入 API Key")
        os.write(master, b"fake-secret-123\x13")
        wait("选择模型 · DeepSeek")
        data = json.loads(credentials.read_text())
        assert data["providers"]["deepseek"] == {"api_key": "fake-secret-123"}
        assert data["active"] is None
        os.write(master, b"\x1b[B\r")
        wait("已选择 DeepSeek · deepseek-v4-pro")
        os.write(master, b"/status\r")
        wait("模型：deepseek-v4-pro")
        os.write(master, b"q\r")
        process.wait(timeout=5)
        assert process.returncode == 0
        assert b"fake-secret-123" not in output
        restored_flags = termios.tcgetattr(slave)
        # macOS sets PENDIN when returning to canonical mode; it is a
        # kernel-managed pending-input marker, not an altered terminal mode.
        for flags in (initial_flags, restored_flags):
            flags[3] &= ~getattr(termios, "PENDIN", 0)
        assert restored_flags == initial_flags
        data = json.loads(credentials.read_text())
        assert data["active"] == "deepseek"
        assert data["providers"]["deepseek"]["model"] == "deepseek-v4-pro"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)
        os.close(slave)
        os.close(master)
