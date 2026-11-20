"""Security boundary tests use fake secrets and temporary directories only."""
import dataclasses
import json
import os
from pathlib import Path
import subprocess

import pytest


def test_child_environment_is_allowlisted(tmp_path):
    from agent_app.security import child_environment
    source = {"LANG": "C", "TERM": "xterm", "PATH": "/malicious", "HOME": "/private-home", "TEST_API_KEY": "fake", "GITHUB_TOKEN": "fake", "SSH_AUTH_SOCK": "/socket", "PYTHONPATH": "/inject", "BASH_ENV": "/inject", "LD_PRELOAD": "/inject", "DYLD_INSERT_LIBRARIES": "/inject", "PAMU_SANDBOX": "off"}
    child = child_environment(source, scratch=tmp_path)
    assert child["LANG"] == "C"
    assert child["TERM"] == "xterm"
    assert child["HOME"] == str(tmp_path / "home")
    assert child["TMPDIR"] == str(tmp_path)
    assert child["PATH"] != source["PATH"]
    assert set(child).isdisjoint(set(source) - {"LANG", "TERM", "PATH", "HOME"})


def test_policy_is_frozen_and_confined_paths_are_protected(tmp_path):
    from agent_app.security import ExecutionPolicy, SecurityDenied
    policy = ExecutionPolicy(tmp_path, tmp_path / "installation", mode="confined")
    with pytest.raises(dataclasses.FrozenInstanceError):
        policy.mode = "off"
    for path in (".pamu/mailboxes/lead/inbox.jsonl", ".settings", ".env", "nested/.env.local", ".ssh/key", "installation/agent_app/x.py"):
        with pytest.raises(SecurityDenied):
            policy.check_path(tmp_path / path)
    for path in (".git/config", ".git/hooks/pre-commit", ".git/modules/x/config"):
        with pytest.raises(SecurityDenied):
            policy.check_path(tmp_path / path, write=True)
    assert policy.check_path(tmp_path / "src/app.py", write=True) == tmp_path / "src/app.py"


def test_policy_protects_runtime_when_workspace_is_install(tmp_path):
    from agent_app.security import ExecutionPolicy, SecurityDenied
    policy = ExecutionPolicy(tmp_path, tmp_path, mode="confined")
    for path in ("agent_app/security/policy.py", "pamu_eval/x.py", ".venv/bin/python", "pyproject.toml"):
        with pytest.raises(SecurityDenied):
            policy.check_path(tmp_path / path, write=True)
    assert policy.check_path(tmp_path / "example.py", write=True) == tmp_path / "example.py"
    assert policy.check_path(tmp_path / "agent_app/security/policy.py") == tmp_path / "agent_app/security/policy.py"


def test_policy_rejects_symlinks_outside_and_protected(tmp_path):
    from agent_app.security import ExecutionPolicy, SecurityDenied
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "escape").symlink_to(tmp_path)
    (root / "alias").symlink_to(root / ".pamu")
    policy = ExecutionPolicy(root, tmp_path / "install", mode="confined")
    for path in (root / "escape/anything", root / "alias/state"):
        with pytest.raises(SecurityDenied):
            policy.check_path(path, write=True)


def test_executor_strips_parent_secrets_and_bounds_descendants(tmp_path, monkeypatch):
    from agent_app.security import ExecutionPolicy, ShellExecutor
    monkeypatch.setenv("TEST_API_KEY", "fake-private-key")
    executor = ShellExecutor(ExecutionPolicy(tmp_path, tmp_path / "install"))
    result = executor.run('printf "%s|%s" "$TEST_API_KEY" "$SSH_AUTH_SOCK"; read value; exit 3')
    assert result.stdout == "|"
    assert result.exit_code == 3
    with pytest.raises(subprocess.TimeoutExpired):
        executor.run("(sleep .2; touch orphan) & sleep 10", timeout=.03)
    import time
    time.sleep(.3)
    assert not (tmp_path / "orphan").exists()


def test_sigint_during_spawn_still_reaps_new_command(tmp_path, monkeypatch):
    import signal
    from agent_app.security import ExecutionPolicy, ShellExecutor

    real_popen = subprocess.Popen
    spawned = []

    def spawn_and_interrupt(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        spawned.append(process)
        signal.raise_signal(signal.SIGINT)
        return process

    monkeypatch.setattr(subprocess, 'Popen', spawn_and_interrupt)
    executor = ShellExecutor(ExecutionPolicy(tmp_path, tmp_path / 'install'))
    try:
        with pytest.raises(KeyboardInterrupt, match='命令已中断'):
            executor.run('sleep 20')
        assert spawned[0].returncode == -signal.SIGKILL
    finally:
        for process in spawned:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()


def test_confined_unavailable_backend_fails_closed(tmp_path, monkeypatch):
    from agent_app.security import ExecutionPolicy, ShellExecutor, SandboxUnavailable
    monkeypatch.setattr("agent_app.security.executor.sys.platform", "unsupported")
    with pytest.raises(SandboxUnavailable):
        ShellExecutor(ExecutionPolicy(tmp_path, tmp_path / "install", mode="confined")).run("touch escaped")
    assert not (tmp_path / "escaped").exists()


def test_config_sandbox_validation(tmp_path):
    from agent_app.config import AppConfig
    assert AppConfig.from_env(tmp_path, environ={"MODEL_ID": "fake"}).sandbox_mode == "off"
    assert AppConfig.from_env(tmp_path, environ={"MODEL_ID": "fake", "PAMU_SANDBOX": "confined"}).sandbox_mode == "confined"
    with pytest.raises(ValueError, match="PAMU_SANDBOX"):
        AppConfig.from_env(tmp_path, environ={"MODEL_ID": "fake", "PAMU_SANDBOX": "unknown"})


def test_runtime_file_handlers_enforce_policy_even_without_hooks(tmp_path):
    from dataclasses import replace
    from agent_app.bootstrap import build_runtime
    from agent_app.config import AppConfig
    config = replace(AppConfig.from_env(tmp_path / "install", tmp_path, {"MODEL_ID": "fake"}), sandbox_mode="confined")
    runtime = build_runtime(config, None)
    (tmp_path / ".env").write_text("fake-secret")
    handlers = runtime.tools.snapshot()[1]
    assert handlers["read_file"](".env").startswith("Error:")
    assert handlers["write_file"](".pamu/mailboxes/evil", "fake").startswith("Error:")
    assert handlers["edit_file"](".env", "fake", "public").startswith("Error:")
    assert ".env" not in handlers["glob"](".*").splitlines()
    assert not (tmp_path / ".pamu/mailboxes/evil").exists()


def test_headless_uses_shared_executor_and_reports_security(tmp_path, monkeypatch):
    from agent_app.execution import execute
    from agent_app.security import ShellExecutor, ShellResult
    called = []
    def run(self, command, **kwargs):
        called.append((self.policy.mode, command, kwargs))
        return ShellResult("shared", "", 0)
    monkeypatch.setattr(ShellExecutor, "run", run)
    replay = tmp_path / "replay.json"
    replay.write_text(json.dumps([{"content": [{"type": "tool_use", "id": "1", "name": "bash", "input": {"command": "echo test"}}], "stop_reason": "tool_use"}, {"content": [{"type": "text", "text": "done"}], "stop_reason": "end_turn"}]))
    result = execute(task="test", workdir=tmp_path, output=tmp_path / "out", model="fake", protocol="anthropic_messages", replay=replay, sandbox="off")
    assert result["status"] == "completed"
    assert called[0][0:2] == ("off", "echo test")
    security = result["security"]
    assert security["sandbox_mode"] == "off"
    assert security["environment"] == "allowlist"
    assert security["hostile_multi_tenant_isolation"] is False


def test_shell_output_is_hard_bounded(tmp_path):
    from agent_app.security import ExecutionPolicy, ShellExecutor
    executor = ShellExecutor(ExecutionPolicy(tmp_path, tmp_path / "install"))
    result = executor.run("yes x | head -c 1000000", timeout=5)
    assert result.exit_code != 0
    assert len(result.stdout.encode()) <= 100000


def test_macos_native_boundary(tmp_path):
    """Real deny/allow probes; never touch personal credentials or the network."""
    import socket
    import sys
    from agent_app.security import ExecutionPolicy, ShellExecutor, SandboxUnavailable
    if sys.platform != "darwin":
        pytest.skip("macOS Seatbelt probe")
    root = tmp_path / "workspace"
    root.mkdir()
    (tmp_path / "sentinel").write_text("fake-outside-marker")
    (root / ".env").write_text("fake-secret-marker")
    (root / ".pamu").mkdir()
    (root / ".pamu" / "state").write_text("control")
    (root / ".git" / "hooks").mkdir(parents=True)
    (root / ".git" / "config").write_text("control")
    executor = ShellExecutor(ExecutionPolicy(root, tmp_path / "install", mode="confined"))
    try:
        result = executor.run("printf allowed > allowed; cat allowed")
    except SandboxUnavailable as exc:
        pytest.skip(f"Seatbelt cannot initialize in this host: {exc}")
    assert result.exit_code == 0, result.stderr
    assert result.stdout == "allowed"
    for command in ("cat ../sentinel", "cat .env", "cat .pamu/state", "cat .PAMU/state", "echo bad > ../outside", "echo bad > .pamu/state", "echo bad > .git/config", "echo bad > .git/hooks/hook"):
        denied = executor.run(command)
        assert denied.exit_code != 0, command
        assert "fake-outside-marker" not in denied.stdout
        assert "fake-secret-marker" not in denied.stdout
    assert not (tmp_path / "outside").exists()
    assert (root / ".pamu" / "state").read_text() == "control"
    # A listening loopback socket proves connection denial rather than an absent
    # remote endpoint. Only temporary fake data is sent if confinement is broken.
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        denied = executor.run(f"/usr/bin/curl --max-time 1 http://127.0.0.1:{server.getsockname()[1]}/")
        assert denied.exit_code != 0
        assert "Operation not permitted" in denied.stderr or "Couldn't connect" in denied.stderr
        server.settimeout(.05)
        with pytest.raises(TimeoutError):
            server.accept()
    parent = executor.run(f"/bin/ps eww -p {os.getpid()}")
    assert parent.exit_code != 0


def test_output_limit_does_not_limit_regular_workspace_files(tmp_path):
    from agent_app.security import ExecutionPolicy, ShellExecutor
    executor = ShellExecutor(ExecutionPolicy(tmp_path, tmp_path / "install"))
    result = executor.run("dd if=/dev/zero of=large.bin bs=200000 count=1 2>/dev/null")
    assert result.exit_code == 0
    assert (tmp_path / "large.bin").stat().st_size == 200000
