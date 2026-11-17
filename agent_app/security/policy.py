"""Immutable execution boundaries. Model input cannot change these objects."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


class SecurityDenied(PermissionError):
    """The requested operation is outside the execution policy."""


class SandboxUnavailable(SecurityDenied):
    """The requested OS boundary could not be established. Never retry naked."""


SAFE_PATH = "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin"
SAFE_ENVIRONMENT = frozenset({"LANG", "LC_ALL", "LC_CTYPE", "TERM", "COLORTERM", "NO_COLOR", "TZ"})
SECRET_DIRECTORIES = frozenset({".pamu", ".ssh", ".aws", ".gnupg", ".codex"})


def _within(path: Path, root: Path) -> bool:
    # Conservative on every platform, including case-insensitive APFS aliases.
    child, parent = str(path).casefold(), str(root).casefold().rstrip('/')
    return child == parent or child.startswith(parent + '/')


def child_environment(source: Mapping[str, str], *, scratch: Path) -> dict[str, str]:
    """Allowlist rather than guessing names of every possible credential."""
    return {
        **{key: val for key, val in source.items() if key in SAFE_ENVIRONMENT},
        "PATH": SAFE_PATH,
        "HOME": str(scratch / "home"),
        "TMPDIR": str(scratch),
        "TMP": str(scratch),
        "TEMP": str(scratch),
    }


@dataclass(frozen=True, slots=True)
class ExecutionPolicy:
    workspace: Path
    install_root: Path
    mode: str = "off"
    protected_paths: tuple[Path, ...] = ()

    def __post_init__(self):
        if self.mode not in {"off", "confined"}:
            raise ValueError("PAMU_SANDBOX must be 'off' or 'confined'")
        object.__setattr__(self, "workspace", Path(self.workspace).resolve())
        object.__setattr__(self, "install_root", Path(self.install_root).resolve())
        object.__setattr__(self, "protected_paths", tuple(Path(path).resolve() for path in self.protected_paths))

    @property
    def runtime_write_roots(self) -> tuple[Path, ...]:
        # When developing in the install repository, retain ordinary source
        # editing while preventing modification of the running control plane.
        if self.install_root == self.workspace:
            return tuple(self.install_root / name for name in ("agent_app", "pamu_eval", ".venv", "pyproject.toml", "uv.lock"))
        return (self.install_root,)

    def check_path(self, path: Path, *, write: bool = False) -> Path:
        resolved = Path(path).resolve()
        if not resolved.is_relative_to(self.workspace):
            raise SecurityDenied("Path escapes workspace")
        if self.mode == "off":
            return resolved
        parts = tuple(part.casefold() for part in resolved.relative_to(self.workspace).parts)
        secret = any(part in SECRET_DIRECTORIES or part == ".settings" or part == ".env" or part.startswith(".env.") for part in parts)
        protected = any(_within(resolved, root) for root in self.protected_paths)
        if write:
            protected |= any(_within(resolved, root) for root in self.runtime_write_roots)
            if ".git" in parts:
                tail = parts[parts.index(".git") + 1:]
                protected |= not tail or tail[0] in {"config", "hooks", "modules", "worktrees"}
            # Protect .git pointer files and replacement of control ancestors.
            protected |= any(_within(root, resolved) for root in (*self.runtime_write_roots, *self.protected_paths))
        elif self.install_root != self.workspace and _within(resolved, self.install_root):
            protected = True
        if secret or protected:
            raise SecurityDenied("Access to protected runtime or credential path denied")
        return resolved

    def metadata(self, *, backend: str | None = None) -> dict:
        return {"sandbox_mode": self.mode, "backend": backend or ("none" if self.mode == "off" else "unavailable"), "environment": "allowlist", "network": "denied" if self.mode == "confined" else "unrestricted", "process_isolation": False, "hostile_multi_tenant_isolation": False, "process_tree_cleanup": "process-group-only; detached descendants may survive", "shell_output_limit_bytes_per_stream": 100000}
