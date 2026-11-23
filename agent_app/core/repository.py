"""Repository facts gathered once per process: the git root and a short snapshot.

The snapshot is never persisted with a session, so a resumed session always sees
the repository as it is when the process starts.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

STATUS_LIMIT = 2000
COMMIT_COUNT = 5


@dataclass(frozen=True, slots=True)
class Repository:
    root: Path | None = None
    snapshot: str = ""


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n...[truncated {len(text) - limit} chars]"


def inspect_repository(workdir: Path, run_git) -> Repository:
    """Describe the enclosing git repository; any git failure leaves the field out."""
    def git(*args):
        ok, output = run_git(list(args))
        if not ok:
            return None
        return "" if output == "(no output)" else output.strip()

    top = git("rev-parse", "--show-toplevel")
    if not top:
        return Repository()
    root = Path(top).resolve()
    if not Path(workdir).resolve().is_relative_to(root):
        return Repository()
    branch = git("branch", "--show-current") or "(detached HEAD)"
    default = (git("symbolic-ref", "--short", "refs/remotes/origin/HEAD") or "").removeprefix("origin/")
    # --branch puts a "## branch...upstream" line first, so stripping the output
    # never eats the significant leading space of a " M path" entry.
    status = git("-c", "status.relativePaths=false", "--no-optional-locks", "status", "--short", "--branch")
    if status is not None and len(status.splitlines()) <= 1:
        status = (status + "\n(clean)").strip()
    commits = git("-c", "log.showSignature=false", "log", "--oneline", f"-{COMMIT_COUNT}")
    lines = [f"Branch: {branch}" + (f" (default: {default})" if default else "")]
    lines.append("Status:\n" + ("(unavailable)" if status is None else _clip(status, STATUS_LIMIT)))
    lines.append("Recent commits:\n" + (commits or "(none)"))
    return Repository(root=root, snapshot="\n".join(lines))
