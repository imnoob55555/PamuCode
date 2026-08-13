# PamuCode

PamuCode is a layered coding-agent runtime extracted into an independent project.

## Setup

```bash
cp .env.example .env
uv sync
```

Set `ANTHROPIC_API_KEY` and `MODEL_ID` in `.env`.

## Run

```bash
uv run python -m agent_app
# or, after installation
uv run pamu
```

## Test

```bash
uv run pytest
```

The implementation is organized under `agent_app/adapters`, `core`, `features`,
and `tools`. `agent_app/bootstrap.py` is the composition root and
`agent_app/cli.py` owns the command-line lifecycle.
