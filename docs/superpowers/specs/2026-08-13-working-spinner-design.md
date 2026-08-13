# PamuCode Working Spinner Design

## Goal

Show a lightweight animated `Working` status while an interactive PamuCode CLI
is waiting for a model response, similar to the feedback provided by Claude Code
and Codex. The indicator must never corrupt streamed model output, leak a worker
thread, or add noise to non-interactive use.

## User experience

After a user submits a prompt, the terminal displays one animated line:

```text
⠋ Working
```

The frame cycles through `⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏`. The indicator:

- starts immediately before each streaming model request;
- disappears before the first non-empty text chunk is printed;
- remains visible until the response completes when the response contains only
  tool-use blocks;
- restarts for the next model request after tool execution;
- is cleared on success, request failure, partial-stream failure, cancellation,
  or context-manager exit;
- is enabled only when its output stream reports `isatty() == True`.

The spinner writes to `stderr`; assistant text remains on `stdout`. Stopping the
spinner clears the entire status line with a carriage return and ANSI erase-line
sequence, then flushes the stream. It does not print a completion line.

## Component boundaries

### Progress protocol

The model adapter depends only on an injected progress factory:

```python
progress: Callable[[str], ContextManager[ProgressHandle]]
```

The returned handle provides an idempotent `stop() -> None`. The adapter does
not import terminal, threading, or CLI modules.

### Terminal implementation

`agent_app/cli_progress.py` owns `TerminalSpinner`. It receives its stream,
frame sequence, interval, thread factory, and wait function as injectable
dependencies where useful for deterministic tests. Its public entry contract is:

```python
terminal_progress(label: str) -> ContextManager[ProgressHandle]
```

For non-TTY streams it returns a no-operation context manager and starts no
thread. The animation thread is daemonized, observes an event, and is joined on
stop with a bounded timeout. A lock prevents animation writes from racing with
the final clear operation.

### Adapter integration

`AnthropicAdapter` gains a progress factory with a no-operation default so
existing tests, explicit construction, and embedded runtimes keep their current
behavior. `create_streaming()` enters `progress("Working")` around the streaming
request and calls `handle.stop()` before printing the first non-empty chunk.
The context manager's `finally` behavior covers every return and exception path.

`create()` remains silent because it is used for internal summarization and
memory work; showing a user-facing spinner for those nested operations would
cause confusing repeated status lines.

### Production wiring

`build_default_runtime()` injects the real terminal progress factory when it
constructs `AnthropicAdapter`. `build_runtime(config, sdk_client)` keeps its
current signature and receives an optional progress factory internally through
a new keyword-only parameter with a no-operation default. Tests and embedders
therefore remain non-interactive unless they explicitly opt in.

## Concurrency and cleanup

- `stop()` is idempotent and safe before or after the animation thread starts.
- Context exit always stops and clears the indicator.
- The thread never survives the request context.
- A failed stream construction is handled the same as a mid-stream exception.
- Keyboard interruption propagates after cleanup.
- The spinner does not share `runtime.stop_event`; its lifetime is one request,
  not the whole application.

## Testing

Focused tests verify:

1. a TTY spinner writes multiple frames, stops, clears once, and joins its
   worker;
2. repeated `stop()` calls are harmless;
3. a non-TTY stream produces no output and creates no thread;
4. the adapter stops the indicator before printing the first text chunk;
5. an adapter exception still exits and clears progress;
6. a tool-only response keeps progress active until final-message completion;
7. default adapter construction remains silent;
8. the complete offline suite remains green and emits no stray spinner output.

Interactive smoke testing runs `pamu` in a temporary workspace, submits a
request through a fake streaming client, and confirms the status is cleared
before assistant text. No live API call is required.

## Documentation

README briefly states that interactive model waits display a `Working` spinner
and that redirected/non-interactive output remains clean. No configuration flag
is introduced in this iteration; TTY detection is the complete enablement rule.
