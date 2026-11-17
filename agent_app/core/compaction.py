"""Persist large background outputs below the workspace result directory."""
from pathlib import Path

from ..config import AppConfig


def _path_below(root: Path, filename: str) -> Path:
    path = (root / filename).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError(f"Persistence path escapes configured root: {filename}")
    return path


def persist_large_output(config: AppConfig, tool_use_id: str, output: str) -> str:
    if len(output) <= config.persist_threshold:
        return output
    config.tool_result_dir.mkdir(parents=True, exist_ok=True)
    path = _path_below(config.tool_result_dir, f"{tool_use_id}.txt")
    if not path.exists():
        path.write_text(output, encoding="utf-8")
    return (
        f"<persisted-output>\nFull output: {path}\nPreview:\n"
        f"{output[:2000]}\n</persisted-output>"
    )
