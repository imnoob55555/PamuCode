"""Path-safe builtin tool implementations.

Every operation receives its allowed workspace root explicitly so importing this
module has no dependency on application configuration or process state.
"""

from __future__ import annotations

import glob
import subprocess
from pathlib import Path

BUILTIN_TOOL_SCHEMAS = [
    {"name": "bash",
     "description": "Run a shell command. If you need the output in the current step, set run_in_background to false. "
                    "For a background command, wait for its result notification before considering another run.",
     "input_schema": {"type": "object",
                      "properties": {"command": {"type": "string"}, "run_in_background": {"type": "boolean", "description": "True runs asynchronously; false waits for the result. If omitted, known slow commands may run in the background."}},
                      "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 1000}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to a file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in a file once.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    {"name": "glob", "description": "Find files matching a glob pattern.",
     "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]}},
    {"name": "load_skill", "description": "Load the content of a skill by name.",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "compact",
     "description": "Summarize earlier conversation and continue with compacted context.",
     "input_schema": {"type": "object",
                      "properties": {"focus": {"type": "string"}},
                      "required": []}},
]


def register_builtin_tools(registry, builtin_dependencies) -> None:
    """Register builtins with callbacks supplied by the composition root."""
    for schema in BUILTIN_TOOL_SCHEMAS:
        name = schema["name"]
        handler = None if name == "compact" else builtin_dependencies[name]
        registry.register(schema, handler)


def resolve_tool_cwd(workdir: Path, cwd: str | Path | None = None) -> Path:
    workspace_root = workdir.resolve()
    base = Path(cwd).resolve() if cwd else workspace_root

    if not base.is_relative_to(workspace_root):
        raise ValueError(f"Tool cwd escapes workspace: {cwd}")
    if not base.is_dir():
        raise ValueError(f"Tool cwd does not exist: {base}")
    return base


def safe_path(
    workdir: Path, path: str, cwd: str | Path | None = None, *, policy=None, write=False
) -> Path:
    base = resolve_tool_cwd(workdir, cwd)
    resolved = (base / path).resolve()

    if not resolved.is_relative_to(base):
        raise ValueError(f"Path escapes working directory: {path}")
    if policy is not None:
        policy.check_path(resolved, write=write)
    return resolved


def run_bash(
    workdir: Path,
    command: str,
    run_in_background: bool = False,
    cwd: str | Path | None = None,
    *, executor=None, timeout=120, json_output=False,
) -> str:
    try:
        base = resolve_tool_cwd(workdir, cwd)
        from ..security import ExecutionPolicy, ShellExecutor
        if executor is None:
            executor = ShellExecutor(ExecutionPolicy(workdir, Path(__file__).resolve().parents[2]))
        result = executor.run(command, cwd=base, timeout=timeout)
        if json_output:
            import json
            from dataclasses import asdict
            return json.dumps(asdict(result))
        output = (result.stdout + result.stderr).strip()
        return output[:50000] if output else "(no output)"
    except subprocess.TimeoutExpired:
        if json_output:
            raise
        return "Error: Timeout (120s)"
    except Exception as exc:
        from ..security import SecurityDenied
        if json_output and isinstance(exc, SecurityDenied):
            raise
        return f"Error: {exc}"


def run_read(
    workdir: Path,
    path: str,
    offset: int = 0,
    limit: int | None = None,
    cwd: str | Path | None = None,
    *, policy=None,
) -> str:
    try:
        checked = safe_path(workdir, path, cwd=cwd, policy=policy)
        if policy is not None and policy.mode == 'confined':
            from ..security.files import open_regular, read_bounded
            with open_regular(policy, checked) as stream:
                text = read_bounded(stream)
        else:
            text = checked.read_text()
        lines = text.splitlines()
        offset = max(0, offset)
        limit = 1000 if limit is None else max(1, min(limit, 1000))
        end = min(offset + limit, len(lines))
        result = lines[offset:end]
        if end < len(lines):
            result.append(
                f"... ({len(lines) - end} more lines);continue with offset={end}"
            )
        return "\n".join(result)
    except Exception as exc:
        return f"Error: {exc}"


def run_write(
    workdir: Path, path: str, content: str, cwd: str | Path | None = None, *, policy=None
) -> str:
    try:
        file_path = safe_path(workdir, path, cwd=cwd, policy=policy, write=True)
        if policy is not None and policy.mode == 'confined':
            from ..security.files import open_regular, replace_text
            with open_regular(policy, file_path, write=True, create=True) as stream:
                replace_text(stream, content)
        else:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as exc:
        return f"Error: {exc}"


def run_edit(
    workdir: Path,
    path: str,
    old_text: str,
    new_text: str,
    cwd: str | Path | None = None,
    *, policy=None,
) -> str:
    try:
        file_path = safe_path(workdir, path, cwd=cwd, policy=policy, write=True)
        if policy is not None and policy.mode == 'confined':
            from ..security.files import open_regular, read_bounded, replace_text
            with open_regular(policy, file_path, write=True) as stream:
                text = read_bounded(stream)
                if old_text not in text:
                    return f"Error: text not found in {path}"
                replace_text(stream, text.replace(old_text, new_text, 1))
            return f"Edited {path}"
        text = file_path.read_text()
        if old_text not in text:
            return f"Error: text not found in {path}"
        file_path.write_text(text.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as exc:
        return f"Error: {exc}"


def run_glob(workdir: Path, pattern: str, cwd: str | Path | None = None, *, policy=None) -> str:
    try:
        base = resolve_tool_cwd(workdir, cwd)
        results = []
        for match in glob.glob(pattern, root_dir=base):
            path = (base / match).resolve()
            if path.is_relative_to(base):
                if policy is not None:
                    try:
                        policy.check_path(path)
                    except PermissionError:
                        continue
                results.append(match)
        return "\n".join(results) if results else "(no matches)"
    except Exception as exc:
        return f"Error: {exc}"
