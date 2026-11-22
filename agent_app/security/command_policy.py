"""Classify model shell commands as allowed, needing approval, or denied.

Only commands made entirely of known read-only programs run without asking.
Anything the parser cannot reason about (substitutions, subshells, writes
through redirection, unknown programs) needs approval, and a few programs
that act on the whole machine are always denied.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import PurePosixPath

ALLOW, ASK, DENY = "allow", "ask", "deny"
_SEPARATOR_CHARS = set(";&|\n")
_REDIRECTS = {">", ">>", "&>", "&>>", ">|", ">&"}
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

READ_ONLY = frozenset({
    "ls", "cat", "head", "tail", "wc", "grep", "egrep", "fgrep", "rg", "fd", "pwd", "echo",
    "printf", "which", "file", "stat", "du", "df", "tree", "sort", "uniq", "cut", "tr",
    "diff", "cmp", "basename", "dirname", "realpath", "date", "true", "false", "test", "[",
    "exit", "cd", "find", "sleep",
})
UNSAFE_OPTIONS = {
    "find": {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf", "-fls"},
    "fd": {"-x", "--exec", "-X", "--exec-batch"},
    "rg": {"--pre"},
    "sort": {"-o", "--output"},
    "tree": {"-o"},
}
GIT_READ_ONLY = frozenset({"status", "diff", "log", "show", "rev-parse", "ls-files", "blame", "describe", "grep"})
GIT_LISTING = {"branch": {"-v", "-vv", "-a", "-r", "--list"}, "remote": {"-v"}, "tag": {"-l", "--list"}}
GIT_UNSAFE_OPTIONS = {"--output", "-O", "--open-files-in-pager", "--ext-diff", "--textconv"}
DENY_PROGRAMS = frozenset({"sudo", "su", "doas", "shutdown", "reboot", "halt", "poweroff"})
SHELL_KEYWORDS = frozenset({"if", "then", "else", "elif", "while", "until", "do", "!", "{"})
CLOSING_KEYWORDS = frozenset({"done", "fi", "}"})
WRAPPERS = frozenset({"command", "builtin", "nohup", "time", "nice", "env", "timeout", "xargs", "exec"})
SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "fish"})
SUBCOMMAND_PROGRAMS = frozenset({
    "git", "npm", "pnpm", "yarn", "bun", "cargo", "go", "pip", "pip3", "uv", "poetry",
    "docker", "kubectl", "brew", "gh", "make",
})
NEVER_REMEMBER = frozenset({
    "rm", "rmdir", "mv", "cp", "chmod", "chown", "dd", "truncate", "shred", "kill", "pkill", "killall",
    "curl", "wget", "scp", "rsync", "ssh", "nc", "ncat", "ftp", "sftp",
    "git push", "git reset", "git clean", "git checkout", "git restore", "git rebase", "git branch",
    "git tag", "git remote", "git config", "git rm", "git commit", "git merge",
})
RUNNER_SUBCOMMANDS = frozenset({"run", "exec", "x", "dlx"})
SENSITIVE = re.compile(
    r"(^|/)\.(ssh|aws|gnupg|pamu|codex)(/|$)|(^|/)\.env(\.|$)|(^|/)\.settings$|(^|/)\.?credentials(\.json)?$|"
    r"id_(rsa|dsa|ecdsa|ed25519)|\.git/(hooks|config)",
    re.IGNORECASE,
)
DANGEROUS_RM_TARGETS = {"/", "/*", "~", "~/", "~/*", "$HOME", "${HOME}", "$HOME/", "${HOME}/", "/.", ".."}


@dataclass(frozen=True)
class CommandDecision:
    action: str
    reason: str = ""
    prefixes: tuple[str, ...] = ()

    @property
    def rememberable(self) -> bool:
        """Approval can be remembered only when every reason is an unknown program."""
        return self.action == ASK and bool(self.prefixes)


def _program(token: str) -> str:
    return PurePosixPath(token).name


def _split(command: str) -> list[list[str]] | None:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()<>\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        tokens = list(lexer)
    except ValueError:
        return None
    segments, current = [], []
    for token in tokens:
        if token and set(token) <= _SEPARATOR_CHARS:
            if current:
                segments.append(current)
            current = []
        else:
            current.append(token)
    if current:
        segments.append(current)
    return segments


def _strip_redirects(tokens: list[str]) -> tuple[list[str], list[str]]:
    """Return the argv without redirections, and the files written by them."""
    argv, written = [], []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in _REDIRECTS or token == "<" or token == "<<" or token == "<<<":
            target = tokens[index + 1] if index + 1 < len(tokens) else ""
            if argv and argv[-1].isdigit() and token != "<":
                argv.pop()
            if token in _REDIRECTS and not (token == ">&" and target.isdigit()) and target != "/dev/null":
                written.append(target)
            index += 2
            continue
        argv.append(token)
        index += 1
    return argv, written


def _effective(argv: list[str]) -> list[str]:
    """Skip shell keywords, variable assignments and wrappers such as ``env`` or ``nohup``."""
    while argv and (argv[0] in SHELL_KEYWORDS or argv[0] in CLOSING_KEYWORDS or _ASSIGNMENT.match(argv[0])):
        argv = argv[1:]
    if argv and argv[0] == "for":
        return []
    while argv and _program(argv[0]) in WRAPPERS:
        wrapper, argv = _program(argv[0]), argv[1:]
        while argv and (argv[0].startswith("-") or _ASSIGNMENT.match(argv[0])):
            argv = argv[1:]
        if wrapper == "timeout" and argv:
            argv = argv[1:]
    return argv


def prefix(argv: list[str]) -> str:
    """The program, plus its subcommand for tools such as ``git`` or ``uv run``."""
    words = [_program(argv[0])]
    rest = argv[1:]
    if words[0] in SUBCOMMAND_PROGRAMS and rest and not rest[0].startswith("-"):
        words.append(rest[0])
        if rest[0] in RUNNER_SUBCOMMANDS and len(rest) > 1 and not rest[1].startswith("-"):
            words.append(_program(rest[1]))
    return " ".join(words)


def _git_decision(argv: list[str]) -> str | None:
    """None when the git invocation is read-only, otherwise the approval reason."""
    args = argv[1:]
    while args and args[0] == "--no-pager":
        args = args[1:]
    if not args or args[0].startswith("-"):
        return "git with global options"
    subcommand, options = args[0], args[1:]
    if any(option.split("=", 1)[0] in GIT_UNSAFE_OPTIONS for option in options):
        return f"git {subcommand} with an option that writes or runs programs"
    if subcommand in GIT_READ_ONLY:
        return None
    if subcommand in GIT_LISTING and all(option in GIT_LISTING[subcommand] for option in options):
        return None
    return f"git {subcommand}"


def _segment(argv: list[str]) -> CommandDecision:
    argv = _effective(argv)
    if not argv:
        return CommandDecision(ALLOW)
    program = _program(argv[0])
    if program in DENY_PROGRAMS or program.startswith("mkfs"):
        return CommandDecision(DENY, f"{program} is never allowed")
    if program == "dd" and any(arg.startswith("of=/dev/") for arg in argv[1:]):
        return CommandDecision(DENY, "dd writing to a device is never allowed")
    if program == "rm":
        recursive = any(arg.startswith("-") and not arg.startswith("--") and ("r" in arg or "R" in arg)
                        or arg in ("--recursive",) for arg in argv[1:])
        if recursive and any(arg in DANGEROUS_RM_TARGETS for arg in argv[1:]):
            return CommandDecision(DENY, "recursive rm of the root or home directory is never allowed")
    if program in SHELLS:
        if "-c" in argv[1:]:
            script = argv[argv.index("-c") + 1] if argv.index("-c") + 1 < len(argv) else ""
            inner = classify(script)
            if inner.action == DENY:
                return inner
        return CommandDecision(ASK, f"runs a nested {program} script")
    if program == "git":
        reason = _git_decision(argv)
        return CommandDecision(ALLOW) if reason is None else _ask(reason, argv)
    if program in READ_ONLY:
        unsafe = UNSAFE_OPTIONS.get(program, set())
        if any(arg.split("=", 1)[0] in unsafe for arg in argv[1:]):
            return _ask(f"{program} with an option that writes or runs programs", argv)
        return CommandDecision(ALLOW)
    return _ask(f"{program} is not a known read-only command", argv)


def _ask(reason: str, argv: list[str]) -> CommandDecision:
    """Ask, offering to remember the prefix unless the program deletes, moves or sends data."""
    key = prefix(argv)
    risky = key in NEVER_REMEMBER or _program(argv[0]) in NEVER_REMEMBER
    return CommandDecision(ASK, reason, () if risky else (key,))


def classify(command: str) -> CommandDecision:
    if not isinstance(command, str) or not command.strip():
        return CommandDecision(ASK, "empty or invalid command")
    segments = _split(command)
    if segments is None:
        return CommandDecision(ASK, "the command could not be parsed")
    reasons, prefixes, structural = [], [], False
    if "$(" in command or "`" in command:
        reasons.append("contains command substitution")
        structural = True
    if any(SENSITIVE.search(token) for tokens in segments for token in tokens):
        reasons.append("may access sensitive files")
        structural = True
    for tokens in segments:
        if "(" in tokens or ")" in tokens:
            reasons.append("runs a subshell")
            structural = True
            tokens = [token for token in tokens if token not in ("(", ")")]
        argv, written = _strip_redirects(tokens)
        if written:
            reasons.append("writes to " + ", ".join(written))
            structural = True
        decision = _segment(argv)
        if decision.action == DENY:
            return decision
        if decision.action == ASK:
            reasons.append(decision.reason)
            if decision.prefixes:
                prefixes.extend(decision.prefixes)
            else:
                structural = True
    if not reasons:
        return CommandDecision(ALLOW)
    unique = tuple(dict.fromkeys(prefixes))
    return CommandDecision(ASK, "; ".join(dict.fromkeys(reasons)), () if structural else unique)
