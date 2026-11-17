"""macOS Seatbelt profile generation. No shell quoting is involved."""
from __future__ import annotations

import json
from pathlib import Path

from .policy import ExecutionPolicy, SECRET_DIRECTORIES


def profile(policy: ExecutionPolicy, scratch: Path) -> str:
    def subpath(path):
        return f"(subpath {json.dumps(str(Path(path).resolve()))})"

    # Default deny includes network, IPC, process inspection/debugging and signals
    # to other processes. Only exec/fork and access to public runtime files are
    # permitted. A same-UID process is not a hostile multi-tenant boundary.
    read_roots = ("/System", "/usr", "/bin", "/sbin", "/Library", "/opt/homebrew", "/private/etc", "/dev", policy.workspace, scratch)
    rules = ["(version 1)", "(deny default)", "(allow process-exec)", "(allow process-fork)", "(allow signal (target self))", "(allow file-read-metadata)", '(allow file-read* (literal "/"))']
    for name in ("kern.ostype", "kern.osrelease", "kern.osversion", "hw.machine", "hw.model", "hw.ncpu", "hw.activecpu", "hw.memsize"):
        rules.append(f"(allow sysctl-read (sysctl-name {json.dumps(name)}))")
    rules.extend(f"(allow file-read* {subpath(root)})" for root in read_roots)
    rules += [f"(allow file-write* {subpath(policy.workspace)} {subpath(scratch)} (literal \"/dev/null\"))"]
    # Regexes cover nested credential/config paths without inspecting contents.
    for name in (*sorted(SECRET_DIRECTORIES), ".settings"):
        escaped = name.replace(".", "\\.")
        rules.append(f'(deny file-read* file-write* (regex {json.dumps(".*/" + escaped + "(/.*)?$")}))')
    rules.append('(deny file-read* file-write* (regex #".*/\\.env(\\..*)?(/.*)?$"))')
    rules.append('(deny file-write* (regex #".*/\\.git(/(config|hooks|modules|worktrees)(/.*)?)?$"))')
    for root in (*policy.runtime_write_roots, *policy.protected_paths):
        rules.append(f"(deny file-write* {subpath(root)})")
    for root in policy.protected_paths:
        rules.append(f"(deny file-read* {subpath(root)})")
    # Granting a write to an ancestor must not allow renaming/deleting the
    # protected directory as a whole. Explicit literal rules cover ancestors.
    for root in (*policy.runtime_write_roots, *policy.protected_paths, policy.workspace / ".pamu"):
        for parent in (root, *root.parents):
            if not parent.is_relative_to(policy.workspace):
                break
            rules.append(f"(deny file-write* (literal {json.dumps(str(parent))}))")
    return "\n".join(rules) + "\n"
