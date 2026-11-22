"""Per-turn file checkpoints so file tool edits can be undone.

Before a turn first changes a file through ``write_file`` or ``edit_file``,
its original bytes (or the fact that it did not exist) are saved under
``.pamu/checkpoints/<session>/<turn>/``. Shell commands are not covered.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from collections.abc import Callable
from pathlib import Path

MANIFEST = "manifest.json"
TURNS_KEPT = 20


def _digest(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except (FileNotFoundError, IsADirectoryError):
        return None


class FileCheckpoints:
    def __init__(
        self,
        workspace: Path,
        root: Path,
        *,
        session_id: Callable[[], str | None],
        turn_id: Callable[[], str | None],
        keep: int = TURNS_KEPT,
        clock: Callable[[], float] = time.time,
    ):
        self.workspace = Path(workspace).resolve()
        self.root = Path(root)
        self.session_id = session_id
        self.turn_id = turn_id
        self.keep = keep
        self.clock = clock

    def _session_dir(self, session: str | None = None) -> Path:
        return self.root / (session or self.session_id() or "unsaved")

    def _turn_dir(self) -> Path:
        return self._session_dir() / (self.turn_id() or "untracked")

    @staticmethod
    def _load(directory: Path) -> dict | None:
        try:
            return json.loads((directory / MANIFEST).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    @staticmethod
    def _save(directory: Path, manifest: dict) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        temporary = directory / (MANIFEST + ".tmp")
        temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(directory / MANIFEST)

    def _relative(self, target: Path) -> str:
        return Path(target).resolve().relative_to(self.workspace).as_posix()

    def before_change(self, target: Path) -> bool:
        """Save the original content the first time this turn touches ``target``.

        Returns whether a new entry was recorded, so a failed change can drop it.
        """
        directory = self._turn_dir()
        manifest = self._load(directory) or {"created": self.clock(), "files": {}}
        relative = self._relative(target)
        if relative in manifest["files"]:
            return False
        entry: dict = {"existed": Path(target).is_file()}
        if entry["existed"]:
            entry["blob"] = f"{len(manifest['files'])}.bin"
            directory.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(target, directory / entry["blob"])
        manifest["files"][relative] = entry
        self._save(directory, manifest)
        self._prune()
        return True

    def drop(self, target: Path) -> None:
        """Forget an entry recorded for a change that did not happen."""
        directory = self._turn_dir()
        manifest = self._load(directory)
        relative = self._relative(target)
        if manifest is None or relative not in manifest["files"]:
            return
        blob = manifest["files"].pop(relative).get("blob")
        if blob:
            (directory / blob).unlink(missing_ok=True)
        if manifest["files"]:
            self._save(directory, manifest)
        else:
            shutil.rmtree(directory, ignore_errors=True)

    def after_change(self, target: Path) -> None:
        """Record the content the turn left behind, to detect later edits."""
        directory = self._turn_dir()
        manifest = self._load(directory)
        relative = self._relative(target)
        if manifest is None or relative not in manifest["files"]:
            return
        manifest["files"][relative]["after"] = _digest(Path(target))
        self._save(directory, manifest)

    def turns(self) -> list[Path]:
        """Checkpointed turns of the current session, newest first."""
        session = self._session_dir()
        if not session.is_dir():
            return []
        dated = []
        for directory in session.iterdir():
            manifest = self._load(directory) if directory.is_dir() else None
            if manifest is not None and manifest.get("files"):
                dated.append((manifest.get("created", 0), directory.name, directory))
        return [directory for *_, directory in sorted(dated, reverse=True)]

    def latest(self) -> Path | None:
        turns = self.turns()
        return turns[0] if turns else None

    def files(self, directory: Path) -> list[str]:
        return sorted((self._load(directory) or {}).get("files", {}))

    def conflicts(self, directory: Path) -> list[str]:
        """Files whose current content differs from what that turn left behind."""
        files = (self._load(directory) or {}).get("files", {})
        return sorted(relative for relative, entry in files.items()
                      if "after" in entry and _digest(self.workspace / relative) != entry["after"])

    def restore(self, directory: Path) -> list[str]:
        """Put every file back as it was before the turn, then forget the turn."""
        files = (self._load(directory) or {}).get("files", {})
        for relative, entry in files.items():
            target = self.workspace / relative
            if entry.get("existed"):
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(directory / entry["blob"], target)
            else:
                target.unlink(missing_ok=True)
        shutil.rmtree(directory, ignore_errors=True)
        return sorted(files)

    def _prune(self) -> None:
        for directory in self.turns()[self.keep:]:
            shutil.rmtree(directory, ignore_errors=True)

    def discard_session(self, session: str | None) -> None:
        if session:
            shutil.rmtree(self._session_dir(session), ignore_errors=True)
