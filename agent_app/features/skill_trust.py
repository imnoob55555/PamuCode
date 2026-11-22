"""Remember which workspaces' own skills the user has agreed to load.

A workspace is trusted for one exact set of project skills: adding or editing
a project skill changes the fingerprint and asks again.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


class SkillTrustStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def _load(self) -> dict[str, str]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        workspaces = data.get("workspaces") if isinstance(data, dict) else None
        return {key: value for key, value in (workspaces or {}).items()
                if isinstance(key, str) and isinstance(value, str)}

    def is_trusted(self, workspace: Path, fingerprint: str) -> bool:
        return self._load().get(str(Path(workspace).resolve())) == fingerprint

    def trust(self, workspace: Path, fingerprint: str) -> None:
        """Atomically record the trusted fingerprint; the file is owner-only."""
        workspaces = self._load()
        workspaces[str(Path(workspace).resolve())] = fingerprint
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.path.parent, delete=False) as handle:
                temporary = Path(handle.name)
                json.dump({"workspaces": workspaces}, handle, indent=2, ensure_ascii=False)
                handle.write("\n")
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


class TrustEverything:
    """Used when PAMU_TRUST_PROJECT_SKILLS is set."""

    def is_trusted(self, _workspace: Path, _fingerprint: str) -> bool:
        return True

    def trust(self, _workspace: Path, _fingerprint: str) -> None:
        return None
