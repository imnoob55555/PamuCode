import errno
import fcntl
import json
import os
import pty
import select
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path

import pytest

# The prompt box marks the frame that accepts input. The welcome panel and
# history separator also draw rules, so they cannot signal readiness.
PROMPT = '❯'.encode()


def _read_until(master_fd, output, marker, deadline, start=0):
    while marker not in output[start:]:
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
    assert marker in output[start:], bytes(output)


def test_resume_replays_dialogue_before_prompt_and_continues_in_same_session(tmp_path):
    from agent_app.features.sessions import checkpoint
    from tests.test_session_runtime import runtime

    original = runtime(tmp_path)
    original.session.history[:] = [
        {'role': 'user', 'content': 'Create add.py'},
        {'role': 'assistant', 'content': 'Saved add.py.\n\nSample:\n    return a + b'},
    ]
    checkpoint(original)
    session_id = original.sessions.session_id
    original.sessions.close()
    script = '''
import sys
from pathlib import Path
from agent_app.bootstrap import build_runtime
from agent_app.cli import main
from agent_app.config import AppConfig
runtime = build_runtime(AppConfig.from_env(Path.cwd(), environ={'MODEL_ID': 'fake'}), None)
main(argv=sys.argv[1:], runtime_factory=lambda: runtime,
     run_turn=lambda rt, query: print(f'CONTINUED {rt.sessions.session_id} {query}'),
     start_threads=lambda _: [], stop_threads=lambda *_: None)
print('EXIT_OK')
'''
    project = Path(__file__).resolve().parents[1]
    master, slave = pty.openpty()
    process = subprocess.Popen([sys.executable, '-c', script, '-r', session_id], cwd=tmp_path,
        env={**os.environ, 'PYTHONPATH': str(project), 'NO_COLOR': '1'}, stdin=slave, stdout=slave, stderr=slave)
    os.close(slave)
    output = bytearray()
    deadline = time.monotonic() + 12
    try:
        _read_until(master, output, PROMPT, deadline)
        transcript = bytes(output).split(PROMPT, 1)[0].decode().replace('\r\n', '\n')
        assert '── 历史对话 ─' in transcript
        assert '> Create add.py\n\n● Saved add.py.\n\n  Sample:\n      return a + b' in transcript
        assert 'PamuCode · AI Coding Agent' not in transcript
        assert b'CONTINUED' not in output
        os.write(master, b'verify\r')
        _read_until(master, output, f'CONTINUED {session_id} verify'.encode(), deadline)
        os.write(master, b'/quit\r')
        _read_until(master, output, b'EXIT_OK', deadline)
        process.wait(timeout=3)
    finally:
        os.close(master)
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)
    assert process.returncode == 0


def test_ctrl_c_during_shell_returns_to_same_session_and_reaps_children(tmp_path):
    project = Path(__file__).resolve().parents[1]
    script = '''
import os, fcntl, termios
from pathlib import Path
from types import SimpleNamespace as NS
from agent_app.bootstrap import build_runtime
from agent_app.config import AppConfig
from agent_app.cli import main
os.setsid()
fcntl.ioctl(0, termios.TIOCSCTTY, 0)
class Adapter:
    calls = 0
    def create_streaming(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return NS(stop_reason='tool_use', content=[
                NS(type='tool_use', id='finished', name='bash', input={'command': 'printf private-output; exit 7'}),
                NS(type='tool_use', id='cancelled', name='bash', input={'command': 'printf CAPTURED_OUTPUT; touch ready; (sleep .6; touch orphan) & sleep 20'}),
                NS(type='tool_use', id='skipped', name='bash', input={'command': 'touch should-not-run'})])
        print('NEXT_TURN_OK', flush=True)
        return NS(stop_reason='end_turn', content=[NS(type='text', text='NEXT_TURN_OK')])
    def create(self, **kwargs):
        return NS(content=[])
config = AppConfig.from_env(Path.cwd(), environ={'MODEL_ID': 'fake'})
runtime = build_runtime(config, None, adapter=Adapter())
main(argv=[], runtime_factory=lambda: runtime, start_threads=lambda rt: [], stop_threads=lambda *args: None)
print('EXIT_OK')
'''
    master, slave = pty.openpty()
    process = subprocess.Popen([sys.executable, '-c', script], cwd=tmp_path,
        env={**os.environ, 'PYTHONPATH': str(project), 'NO_COLOR': '1'}, stdin=slave, stdout=slave, stderr=slave)
    os.close(slave)
    output = bytearray()
    deadline = time.monotonic() + 12
    try:
        _read_until(master, output, PROMPT, deadline)
        os.write(master, b'first\r')
        _read_until(master, output, '● Ran printf private-output; exit 7 (exit 7)'.encode(), deadline)
        _read_until(master, output, '● Running printf CAPTURED_OUTPUT; touch ready'.encode(), deadline)
        while not (tmp_path / 'ready').exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert (tmp_path / 'ready').exists()
        os.write(master, b'\x03')
        _read_until(master, output, '已中断当前轮次'.encode(), deadline)
        os.write(master, b'second\r')
        _read_until(master, output, b'NEXT_TURN_OK', deadline)
        os.write(master, b'/quit\r')
        _read_until(master, output, b'EXIT_OK', deadline)
        process.wait(timeout=3)
    finally:
        os.close(master)
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)
    assert process.returncode == 0
    assert b'Traceback' not in output
    assert ('● Running printf private-output; exit 7\r\x1b[2K● Ran printf private-output; exit 7 (exit 7)'
            .encode()) in output
    assert '● Interrupted printf CAPTURED_OUTPUT; touch ready'.encode() in output
    assert b'Ran printf CAPTURED_OUTPUT' not in output and b'Running touch should-not-run' not in output
    assert b'[HOOK]' not in output and b'[inject]' not in output and b'> bash' not in output
    snapshots = list((tmp_path / '.pamu' / 'sessions').glob('*.json'))
    assert len(snapshots) == 1
    snapshot = json.loads(snapshots[0].read_text())
    assert snapshot['status'] == 'idle'
    history = snapshot['history']
    assert any(m['content'] == 'second' for m in history)
    results = [b for m in history if isinstance(m['content'], list)
               for b in m['content'] if b['type'] == 'tool_result']
    assert len(results) == 3
    assert results[0]['content'] == 'private-output'
    assert results[1]['is_error'] and 'CAPTURED_OUTPUT' in results[1]['content']
    assert results[2]['is_error'] and '未执行' in results[2]['content']
    time.sleep(.6)
    assert not (tmp_path / 'orphan').exists()
    assert not (tmp_path / 'should-not-run').exists()


def test_permission_hooks_prompt_before_shell_and_file_changes(tmp_path):
    (tmp_path / 'note.txt').write_text('before\n')
    project = Path(__file__).resolve().parents[1]
    script = '''
from pathlib import Path
from types import SimpleNamespace as NS
from agent_app.bootstrap import build_runtime
from agent_app.cli import main
from agent_app.config import AppConfig

class Adapter:
    sent_tools = False
    def create_streaming(self, **kwargs):
        if not self.sent_tools:
            self.sent_tools = True
            return NS(stop_reason='tool_use', content=[
                NS(type='tool_use', id='declined', name='bash', input={'command': 'printf "rm declined" > declined.txt'}),
                NS(type='tool_use', id='allowed', name='bash', input={'command': 'printf "rm allowed" > allowed.txt'}),
                NS(type='tool_use', id='edit', name='edit_file',
                   input={'path': 'note.txt', 'old_text': 'before', 'new_text': 'after'}),
            ])
        print('TURN_DONE', flush=True)
        return NS(stop_reason='end_turn', content=[NS(type='text', text='TURN_DONE')])
    def create(self, **kwargs):
        return NS(content=[])

config = AppConfig.from_env(Path.cwd(), environ={'MODEL_ID': 'fake'})
runtime = build_runtime(config, None, adapter=Adapter())
main(argv=[], runtime_factory=lambda: runtime, credentials_path=Path('fake-credentials.json'),
     start_threads=lambda _: [], stop_threads=lambda *_: None)
print('EXIT_OK')
'''
    master, slave = pty.openpty()
    process = subprocess.Popen([sys.executable, '-c', script], cwd=tmp_path,
        env={**os.environ, 'PYTHONPATH': str(project), 'NO_COLOR': '1'}, stdin=slave, stdout=slave, stderr=slave)
    os.close(slave)
    output = bytearray()
    deadline = time.monotonic() + 12
    prompt = b'Allow this change? [y/N]'
    try:
        _read_until(master, output, PROMPT, deadline)
        os.write(master, b'check approvals\r')
        _read_until(master, output, prompt, deadline)
        assert b'Potentially destructive command' in output
        assert not (tmp_path / 'declined.txt').exists()
        next_prompt = output.index(prompt) + len(prompt)
        os.write(master, b'n\r')
        _read_until(master, output, prompt, deadline, start=next_prompt)
        assert not (tmp_path / 'allowed.txt').exists()
        assert b'Running printf "rm allowed"' not in output
        os.write(master, b'y\r')
        _read_until(master, output, b'Apply change? [y/N]', deadline)
        assert '● Ran printf "rm allowed" > allowed.txt'.encode() in output
        assert b'-before' in output and b'+after' in output
        assert (tmp_path / 'note.txt').read_text() == 'before\n'
        os.write(master, b'y\r')
        _read_until(master, output, b'TURN_DONE', deadline)
        os.write(master, b'/quit\r')
        _read_until(master, output, b'EXIT_OK', deadline)
        process.wait(timeout=3)
    finally:
        os.close(master)
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)
    assert process.returncode == 0
    assert b'Running printf "rm declined"' not in output
    assert not (tmp_path / 'declined.txt').exists()
    assert (tmp_path / 'allowed.txt').read_text() == 'rm allowed'
    assert (tmp_path / 'note.txt').read_text() == 'after\n'


@pytest.mark.parametrize("short", [False, True])
def test_background_shell_replaces_running_status_without_disturbing_input(tmp_path, short):
    project = Path(__file__).resolve().parents[1]
    script = '''
import os
from pathlib import Path
from types import SimpleNamespace as NS
from agent_app.bootstrap import build_runtime
from agent_app.cli import main
from agent_app.config import AppConfig

class Adapter:
    calls = 0
    def create_streaming(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            command = os.environ['PAMU_PTY_SHELL_COMMAND']
            return NS(stop_reason='tool_use', content=[
                NS(type='tool_use', id='bg', name='bash',
                   input={'command': command, 'run_in_background': True}),
            ])
        last_input = kwargs['messages'][-1]['content']
        if isinstance(last_input, list):
            last_input = next((b['text'] for b in last_input if b['type'] == 'text'), '')
        marker = 'INPUT_INTACT' if last_input == 'next action' else 'WAITING_AT_PROMPT'
        print(marker, flush=True)
        return NS(stop_reason='end_turn', content=[NS(type='text', text=marker)])
    def create(self, **kwargs):
        return NS(content=[])

config = AppConfig.from_env(Path.cwd(), environ={'MODEL_ID': 'fake'})
runtime = build_runtime(config, None, adapter=Adapter())
main(argv=[], runtime_factory=lambda: runtime, credentials_path=Path('fake-credentials.json'),
     start_threads=lambda _: [], stop_threads=lambda *_: None)
print('EXIT_OK')
'''
    master, slave = pty.openpty()
    command = 'printf ready' if short else 'while [ ! -f release ]; do sleep .02; done; printf ready'
    process = subprocess.Popen([sys.executable, '-c', script], cwd=tmp_path,
        env={**os.environ, 'PYTHONPATH': str(project), 'TERM': 'xterm', 'NO_COLOR': '1',
             'PAMU_PTY_SHELL_COMMAND': command},
        stdin=slave, stdout=slave, stderr=slave)
    os.close(slave)
    output = bytearray()
    deadline = time.monotonic() + 12
    running = f'Running {command}'.encode()
    finished = f'● Ran {command}'.encode()
    try:
        _read_until(master, output, PROMPT, deadline)
        os.write(master, b'start background\r')
        _read_until(master, output, b'WAITING_AT_PROMPT', deadline)
        at_prompt = output.index(b'WAITING_AT_PROMPT') + len(b'WAITING_AT_PROMPT')
        _read_until(master, output, running, deadline, start=at_prompt)
        os.write(master, b'next')
        (tmp_path / 'release').touch()
        _read_until(master, output, finished, deadline)
        os.write(master, b' action\r')
        _read_until(master, output, b'INPUT_INTACT', deadline)
        os.write(master, b'/quit\r')
        _read_until(master, output, b'EXIT_OK', deadline)
        process.wait(timeout=3)
    finally:
        (tmp_path / 'release').touch()
        os.close(master)
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)

    assert process.returncode == 0
    assert output.count(finished) == 1
    assert running not in output[output.index(finished) + len(finished):]


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
    process = subprocess.Popen(  # noqa: S603 - fixed interpreter and script
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
        _read_until(master_fd, output, PROMPT, deadline)
        os.write(master_fd, b"hello\n")
        _read_until(master_fd, output, b"Working", deadline)
        os.write(release_write, b"1")
        _read_until(master_fd, output, b"FAKE_ASSISTANT_TEXT", deadline)
        os.write(master_fd, b"/quit\n")
        _read_until(master_fd, output, b"SPINNER_THREADS=0", deadline)
        process.wait(timeout=max(0.1, deadline - time.monotonic()))
    finally:
        os.close(release_write)
        os.close(master_fd)
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)

    merged = bytes(output)
    spinner_at = merged.index(b"Working")
    clear_at = merged.index(b"\r\x1b[2K", spinner_at)
    text_at = merged.index(b"FAKE_ASSISTANT_TEXT", clear_at)
    assert spinner_at < clear_at < text_at
    assert b'[HOOK]' not in merged
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
    process = subprocess.Popen(  # noqa: S603 - fixed interpreter and script
        [sys.executable, "-c", script, str(credentials)],
        stdin=slave, stdout=slave, stderr=slave, cwd=tmp_path, env=environment,
    )
    output = bytearray()
    deadline = time.monotonic() + 15
    def wait(marker, start=0):
        _read_until(master, output, marker.encode(), deadline, start)
    try:
        wait("请输入 /login")
        wait("❯")
        assert "选择 Provider".encode() not in output
        os.write(master, b"/")
        wait("配置模型服务与认证")
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

        # Select /model directly from slash hints, then select a model.
        start = len(output)
        os.write(master, b"/\x1b[B\x1b[B\r")
        wait("选择模型 · DeepSeek", start)
        os.write(master, b"\x1b[B\r")
        wait("模型已切换为 deepseek-v4-flash", start)

        data = json.loads(credentials.read_text())
        data["providers"]["anthropic"] = {
            "api_key": "fake-second-provider", "model": "claude-sonnet-4-6",
        }
        credentials.write_text(json.dumps(data))
        start = len(output)
        os.write(master, b"/\x1b[B\x1b[B\x1b[B\r")
        wait("切换 Provider", start)
        os.write(master, b"\x1b[B\r")
        wait("供应商已切换为 Anthropic 官方", start)
        os.write(master, b"/status\r")
        wait("模型：claude-sonnet-4-6", start)
        os.write(master, b"/quit\r")
        # Drain redraw/shutdown output so the child cannot block on a full PTY.
        wait("Shutting down PamuCode…", start)
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
        assert data["active"] == "anthropic"
        assert data["providers"]["deepseek"]["model"] == "deepseek-v4-flash"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)
        os.close(slave)
        os.close(master)


def test_resume_picker_restores_and_quits_without_model_request(tmp_path):
    from tests.test_session_runtime import runtime
    from agent_app.features.sessions import checkpoint
    from agent_app.features.session_store import SessionStore

    original = runtime(tmp_path)
    original.model.base_url = 'https://api.anthropic.com'
    original.session.history[:] = [{'role': 'user', 'content': 'PTY restored question'}]
    checkpoint(original)
    saved = original.sessions.store.load(original.sessions.session_id)
    original.sessions.close()
    script = '''
import sys
from pathlib import Path
from types import SimpleNamespace
from agent_app import bootstrap
from agent_app.cli import main
class Client:
    def __init__(self, **kw):
        self.messages = SimpleNamespace(create=self.fail, stream=self.fail)
    def fail(self, **kw):
        raise AssertionError('resume must not send a model request')
sys.modules['anthropic'] = SimpleNamespace(Anthropic=Client)
bootstrap.load_runtime_environment = lambda: {'ANTHROPIC_API_KEY': 'fake-pty-only'}
raise SystemExit(main(argv=['resume'], credentials_path=Path(sys.argv[1])))
'''
    environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]),
                       TERM='xterm-256color', PROMPT_TOOLKIT_NO_CPR='1')
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 28, 110, 0, 0))
    process = subprocess.Popen(
        [sys.executable, '-c', script, str(tmp_path / 'missing-credentials.json')],
        stdin=slave, stdout=slave, stderr=slave, cwd=tmp_path, env=environment,
    )
    output = bytearray()
    deadline = time.monotonic() + 15
    try:
        _read_until(master, output, '选择会话'.encode(), deadline)
        _read_until(master, output, b'PTY restored question', deadline)
        os.write(master, b'\r')
        _read_until(master, output, '已恢复会话'.encode(), deadline)
        _read_until(master, output, PROMPT, deadline, start=output.index('已恢复会话'.encode()))
        os.write(master, b'/status\r')
        _read_until(master, output, ('会话：' + saved['id']).encode(), deadline)
        os.write(master, b'/quit\r')
        _read_until(master, output, 'Shutting down PamuCode'.encode(), deadline)
        process.wait(timeout=5)
        assert process.returncode == 0
        assert b'fake-pty-only' not in output
        assert SessionStore(tmp_path).load(saved['id']) == saved
        SessionStore(tmp_path).acquire(saved['id']).close()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)
        os.close(slave)
        os.close(master)
