# PamuCode Independent Project Design

## Goal

Create `/Users/wh/python/PamuCode` as a standalone Python project containing the
validated layered agent application extracted from `learn-claude-code`. PamuCode
must not retain the course repository layout, Git history, `homework` namespace,
or root-level `BaseAgent.py` compatibility entry point.

## Repository boundary

The new repository contains only the application, its focused tests, runtime
configuration examples, and project documentation:

```text
PamuCode/
├── agent_app/
│   ├── __main__.py
│   ├── adapters/
│   ├── core/
│   ├── features/
│   ├── tools/
│   ├── bootstrap.py
│   ├── cli.py
│   ├── config.py
│   └── runtime.py
├── tests/
├── docs/
├── pyproject.toml
├── .env.example
├── .gitignore
└── README.md
```

Numbered lesson directories, legacy requirements, unrelated docs, generated
runtime state, secrets, and the source repository's `.git` metadata are excluded.

## Entry points and API

There is no root `BaseAgent.py`. The supported entry points are:

```bash
python -m agent_app
pamu
```

`agent_app/__main__.py` delegates only to `agent_app.cli.main`. The `pamu`
command is registered in `pyproject.toml` as `agent_app.cli:main`. Internal
modules remain implementation details rather than a compatibility API.

## Migration

Application files are copied from validated source commit `cae7729`. The
`homework/agent_app` directory becomes top-level `agent_app`, and test imports
change from `homework.agent_app.*` to `agent_app.*`. Tests tied specifically to
the removed legacy script path are adapted to exercise `python -m agent_app`.
No agent behavior is intentionally changed during extraction.

## Dependencies and configuration

The project targets Python 3.13 and declares only direct runtime dependencies:
Anthropic SDK, python-dotenv, and PyYAML. Pytest is a development dependency.
`.env.example` documents required model/provider variables without real secrets.
`.gitignore` excludes `.env`, virtual environments, caches, transcripts, memory,
mailboxes, scheduled jobs, task state, and other generated runtime data.

## Verification

Completion requires:

1. the complete migrated pytest suite passes without live API calls;
2. all modules compile;
3. importing bootstrap and CLI in an empty directory creates no files or threads;
4. `MODEL_ID=dummy python -m agent_app` starts and exits cleanly with `q`;
5. no source or test imports the `homework` namespace;
6. no root `BaseAgent.py` exists;
7. the new Git repository is independent and has a clean initial commit.

## Source preservation

The existing `learn-claude-code` main workspace and its uncommitted changes are
never modified. The former refactor worktree remains available until PamuCode has
passed verification, after which its cleanup can be decided separately.
