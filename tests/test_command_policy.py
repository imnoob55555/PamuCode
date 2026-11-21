import pytest

from agent_app.security.command_policy import ALLOW, ASK, DENY, classify, prefix


@pytest.mark.parametrize("command", [
    "ls -la", "cat README.md | head -n 5", "git status && git diff --stat", "rg -n TODO agent_app",
    "find . -name '*.py'", "echo hi 2>/dev/null", "printf ok; exit 7", "git --no-pager log -3",
    "git branch -a", "git remote -v", "wc -l < input.txt", "sleep 1",
    "while [ ! -f release ]; do sleep .02; done; printf ready",
    "for f in a b; do echo $f; done", "if test -f x; then cat x; else echo none; fi",
])
def test_read_only_commands_run_without_approval(command):
    assert classify(command).action == ALLOW


@pytest.mark.parametrize("command,reason", [
    ("echo x > file.py", "writes to file.py"),
    ("cat a >> b", "writes to b"),
    ("ls 2> errors.log", "writes to errors.log"),
    ("echo $(whoami)", "command substitution"),
    ("echo `id`", "command substitution"),
    ("(cd /tmp; ls)", "subshell"),
    ("git push --force", "git push"),
    ("git -c core.pager=sh log", "git with global options"),
    ("git diff --output=patch.diff", "writes or runs programs"),
    ("find . -name x -delete", "writes or runs programs"),
    ("fd -x rm", "writes or runs programs"),
    ("sort -o out.txt in.txt", "writes or runs programs"),
    ("sed -i s/a/b/ f", "sed is not a known read-only command"),
    ("wget https://example.com", "wget is not a known read-only command"),
    ("python -c 'print(1)'", "python is not a known read-only command"),
    ("bash -c 'ls'", "nested bash script"),
    ("echo 'unterminated", "could not be parsed"),
])
def test_writes_unknown_programs_and_unparseable_commands_need_approval(command, reason):
    decision = classify(command)
    assert decision.action == ASK
    assert reason in decision.reason


@pytest.mark.parametrize("command", [
    "sudo ls", "s\\udo ls", "/usr/bin/sudo ls", "env FOO=1 sudo ls", "nohup sudo ls", "timeout 5 sudo ls",
    "ls && sudo rm x", "shutdown -h now", "mkfs.ext4 /dev/sda", "dd if=/dev/zero of=/dev/disk2",
    "rm -rf /", "rm -fr ~", "rm -r -f \"$HOME\"", "bash -c 'sudo id'",
])
def test_machine_wide_commands_are_denied_even_when_disguised(command):
    assert classify(command).action == DENY


def test_shell_keywords_do_not_hide_the_commands_they_run():
    assert classify("while true; do rm -rf build; done").action == ASK
    assert classify("if true; then sudo id; fi").action == DENY
    assert classify("for f in $(ls); do echo $f; done").action == ASK


def test_echo_mentioning_sudo_is_not_denied():
    assert classify("echo sudo").action == ALLOW


def test_only_unknown_programs_can_be_remembered_for_the_session():
    decision = classify("uv run pytest -q && npm test")
    assert decision.prefixes == ("uv run pytest", "npm test") and decision.rememberable
    assert classify("touch a.txt").prefixes == ("touch",)
    assert not classify("uv run pytest > log.txt").rememberable
    assert not classify("python -c \"$(cat x)\"").rememberable
    assert not classify("ls").rememberable


@pytest.mark.parametrize("argv,expected", [
    (["git", "push", "origin"], "git push"),
    (["/usr/local/bin/uv", "run", "pytest", "-q"], "uv run pytest"),
    (["npm", "-g", "install"], "npm"),
    (["make", "test"], "make test"),
    (["touch", "a.txt"], "touch"),
])
def test_prefix_keeps_subcommands_for_tool_launchers(argv, expected):
    assert prefix(argv) == expected
