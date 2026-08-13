# PamuCode Current-Directory Workspace Design

## Goal

Make the globally installed `pamu` command behave like Claude Code: invoking it
from any directory makes that directory the active workspace. PamuCode's own
installation location must not determine which project its tools operate on.

## Path ownership

PamuCode distinguishes three roots:

1. **Installation root** — the directory containing the installed PamuCode
   package. It locates application-owned resources only and is never the tool
   workspace.
2. **Workspace root** — `Path.cwd().resolve()` at CLI startup. File tools, Git,
   hooks, prompts, teammates, tasks, worktrees, and scheduler activity use this
   directory.
3. **State root** — `<workspace>/.pamu`. All hidden project runtime state lives
   below this one directory.

The project layout is:

```text
<workspace>/
├── project files
├── skills/
└── .pamu/
    ├── .env
    ├── .gitignore
    ├── memory/
    │   └── MEMORY.md
    ├── transcripts/
    ├── task_outputs/
    │   └── tool-results/
    ├── tasks/
    ├── mailboxes/
    ├── worktrees/
    └── scheduled_tasks.json
```

`skills/` remains a visible project directory because it is authored project
content rather than generated runtime state.

## Configuration loading

Model/provider configuration has three layers, from lowest to highest priority:

```text
~/.config/pamucode/.env
→ <workspace>/.pamu/.env
→ variables explicitly present in the launching process environment
```

PamuCode reads both dotenv files with `dotenv_values()` and merges the resulting
mappings in the order shown above. The launching process environment is applied
last and therefore remains authoritative. This isolated mapping is passed into
runtime configuration without mutating `os.environ`, so sequential runtimes do
not inherit values loaded for an earlier workspace.

Supported variables remain `ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL`,
`ANTHROPIC_AUTH_TOKEN`, `MODEL_ID`, and `FALLBACK_MODEL_ID`. When
`ANTHROPIC_BASE_URL` is configured, PamuCode passes the selected base URL and API
key explicitly to the Anthropic-compatible client and does not pass an ambient
`ANTHROPIC_AUTH_TOKEN`.

## Runtime state mapping

`AppConfig` receives the installation root and workspace root separately. Its
paths map as follows:

```text
repo_root              = installation root
workdir                = workspace root
skills_dir             = <workspace>/skills
memory_dir             = <workspace>/.pamu/memory
memory_index           = <workspace>/.pamu/memory/MEMORY.md
transcripts_dir        = <workspace>/.pamu/transcripts
tool_result_dir        = <workspace>/.pamu/task_outputs/tool-results
task_dir               = <workspace>/.pamu/tasks
mailbox_dir            = <workspace>/.pamu/mailboxes
scheduled_tasks_path   = <workspace>/.pamu/scheduled_tasks.json
worktrees_dir          = <workspace>/.pamu/worktrees
```

The state root contains a generated `.gitignore`:

```gitignore
*
!.gitignore
```

PamuCode creates this file only when absent and never overwrites a user's
existing `.pamu/.gitignore`.

## CLI behavior

`build_default_runtime()` captures `Path.cwd()` once at startup and passes it to
configuration construction. A turn cannot silently change workspaces if the
process later calls `os.chdir()`.

The existing injectable `build_runtime(config, sdk_client)` contract remains
unchanged. Tests and embedders can continue constructing an explicit
`AppConfig` without reading dotenv files or the process current directory.

## Security and errors

- Real `.env` files remain untracked by PamuCode itself.
- Generated project state is ignored locally by `.pamu/.gitignore`.
- No API key or environment value is printed during startup.
- A missing `MODEL_ID` continues to fail during `AppConfig` construction with
  the existing explicit environment lookup.
- State directories are created only when building a runtime, never when
  importing modules.
- Static symlink and path validation prevents accidental state escape, but
  PamuCode is not a sandbox against the workspace owner concurrently mutating
  paths while it runs. This boundary does not weaken file-tool workspace
  containment.

## Verification

Completion requires:

1. `AppConfig` maps every generated path beneath `<workspace>/.pamu` while
   keeping `skills/` at `<workspace>/skills`;
2. global, project, and process configuration priority is verified without
   exposing secret values;
3. starting in two different temporary directories creates independent runtime
   state under each directory;
4. file tools and Git use the invocation directory;
5. an existing `.pamu/.gitignore` is preserved;
6. imports remain side-effect free;
7. the full offline suite passes;
8. both `python -m agent_app` and the globally installed `pamu` command operate
   on the current directory;
9. README documents global installation and configuration.

## Compatibility

Existing state stored directly under `.memory`, `.tasks`, `.mailboxes`,
`.transcripts`, `.task_outputs`, `.worktrees`, or `.scheduled_tasks.json` is not
automatically migrated. Users may move it manually into `.pamu/`. This avoids
surprising file mutations during startup and keeps this change focused on the
new workspace contract.
