"""Terminal progress for model requests and shell commands."""

from __future__ import annotations

import itertools
import os
import shutil
import sys
import threading
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from time import monotonic, sleep
from typing import Any

from prompt_toolkit.utils import get_cwidth

DEFAULT_FRAMES = tuple("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏")
_TERMINAL_LOCK = threading.Lock()
_visible_spinner: TerminalSpinner | None = None
_CLEAR_LINE = "\r\033[2K"
_RUNNING_VISIBLE_SECONDS = .25
_model_line_open = False
_approval_open = False
_pending_shell_status: list[str] = []
_foreground_shell: tuple[Any, float] | None = None
_invalidate_shell_status: Callable[[], None] | None = None


@dataclass(slots=True)
class _BackgroundStatus:
    line: str
    shown: threading.Event = field(default_factory=threading.Event)
    visible_at: float = 0.0


_background_shell_status: dict[int, _BackgroundStatus] = {}


def _erase_spinner() -> None:
    global _visible_spinner
    if _visible_spinner is not None:
        spinner, _visible_spinner = _visible_spinner, None
        spinner._erase_line()


def _flush_shell_status() -> None:
    if _model_line_open or _approval_open or _foreground_shell is not None or not _pending_shell_status:
        return
    try:
        _erase_spinner()
        for line in _pending_shell_status:
            print(line, flush=True)
    except (OSError, ValueError):
        # A closed output stream must not interrupt process cleanup.
        pass
    finally:
        _pending_shell_status.clear()


def _fit_terminal_line(text: str) -> str:
    # Leave a column free so clearing the line cannot leave wrapped fragments.
    width = max(1, shutil.get_terminal_size(fallback=(80, 24)).columns - 1)
    if get_cwidth(text) <= width:
        return text
    used = 0
    for index, char in enumerate(text):
        used += get_cwidth(char)
        if used >= width:
            return text[:index] + '…'
    return text


def _background_status(*, mark_visible: bool) -> str:
    if not _background_shell_status:
        return ''
    latest = next(reversed(_background_shell_status.values()))
    if mark_visible and not latest.shown.is_set():
        latest.visible_at = monotonic()
        latest.shown.set()
    extra = len(_background_shell_status) - 1
    return f'{latest.line} (+{extra})' if extra else latest.line


def background_shell_status(*, mark_visible: bool = True) -> str:
    with _TERMINAL_LOCK:
        return _background_status(mark_visible=mark_visible)


@contextmanager
def watch_shell_status(invalidate: Callable[[], None]):
    """Refresh the REPL's running line when a shell worker changes state."""
    global _invalidate_shell_status
    with _TERMINAL_LOCK:
        previous, _invalidate_shell_status = _invalidate_shell_status, invalidate
    try:
        yield
    finally:
        with _TERMINAL_LOCK:
            _invalidate_shell_status = previous


def _supports_status_line(stream) -> bool:
    try:
        return stream.isatty() and os.environ.get('TERM') != 'dumb'
    except (AttributeError, OSError, ValueError):
        return False


def _show_foreground_shell(line: str) -> None:
    global _foreground_shell
    stream = sys.stdout
    if not _supports_status_line(stream):
        return
    try:
        _erase_spinner()
        stream.write(_fit_terminal_line(line))
        stream.flush()
    except (OSError, ValueError):
        return
    _foreground_shell = (stream, monotonic())


def _clear_foreground_shell() -> None:
    global _foreground_shell
    stream, _ = _foreground_shell
    _foreground_shell = None
    try:
        stream.write(_CLEAR_LINE)
        stream.flush()
    except (OSError, ValueError):
        pass


def _keep_running_visible(worker: int, main_thread: bool) -> None:
    with _TERMINAL_LOCK:
        foreground = _foreground_shell if main_thread else None
        background = _background_shell_status.get(worker)
        approval_open = _approval_open
    if foreground is not None:
        _, visible_at = foreground
    elif (background is not None and not approval_open and _supports_status_line(sys.stdout)
          and background.shown.wait(_RUNNING_VISIBLE_SECONDS)):
        visible_at = background.visible_at
    else:
        return
    remaining = visible_at + _RUNNING_VISIBLE_SECONDS - monotonic()
    if remaining > 0:
        # Process cleanup has already completed. Let the terminal paint before
        # publishing the final status, without holding the output lock.
        sleep(remaining)


def show_shell_status(event: str, command: str, exit_code: int | None) -> None:
    from .features.session_codec import safe_terminal_text

    label = {'running': 'Running', 'finished': 'Ran', 'interrupted': 'Interrupted',
             'timed_out': 'Timed out', 'failed': 'Failed'}[event]
    text = safe_terminal_text(command)
    suffix = f' (exit {exit_code})' if exit_code not in (None, 0) else ''
    main_thread = threading.current_thread() is threading.main_thread()
    worker = threading.get_ident()
    if event == 'finished':
        _keep_running_visible(worker, main_thread)
    with _TERMINAL_LOCK:
        if event == 'running':
            single_line = text.replace('\n', ' ').expandtabs()
            line = f'{label} {single_line}'
            if main_thread and not (_model_line_open or _approval_open or _invalidate_shell_status):
                _show_foreground_shell(line)
                return
            # Shell dispatch is synchronous inside each worker, including
            # separate workers running identical commands concurrently.
            _background_shell_status[worker] = _BackgroundStatus(line)
        else:
            command_text = text.replace('\n', '\n  ')
            line = f'{label} {command_text}{suffix}'
            _background_shell_status.pop(worker, None)
            if main_thread and _foreground_shell is not None:
                _clear_foreground_shell()
                _pending_shell_status.insert(0, line)
            else:
                _pending_shell_status.append(line)
            _flush_shell_status()
        invalidate = _invalidate_shell_status
    if invalidate is not None:
        try:
            invalidate()
        except RuntimeError:
            # The prompt can close its event loop while a worker is finishing.
            pass


def terminal_confirmation(prompt: str) -> str:
    """Keep command notices below the permission question and its input."""
    global _approval_open
    try:
        with _TERMINAL_LOCK:
            _approval_open = True
            _erase_spinner()
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        print()
        raise
    finally:
        with _TERMINAL_LOCK:
            _approval_open = False
            _flush_shell_status()


def write_model_text(text: str) -> None:
    global _model_line_open
    with _TERMINAL_LOCK:
        print(text, end='', flush=True)
        _model_line_open = not text.endswith('\n')
        _flush_shell_status()


def finish_model_text() -> None:
    global _model_line_open
    with _TERMINAL_LOCK:
        if _model_line_open:
            _model_line_open = False
            print()
        _flush_shell_status()


class TerminalSpinner:
    """Animate a status label on an interactive terminal until stopped."""

    def __init__(
        self,
        label: str,
        *,
        stream=None,
        frames=DEFAULT_FRAMES,
        interval: float = 0.08,
        event_factory: Callable[[], Any] = threading.Event,
        thread_factory: Callable[..., Any] = threading.Thread,
    ) -> None:
        self.label = label
        self.stream = sys.stderr if stream is None else stream
        self.frames = tuple(frames)
        self.interval = interval
        self._event_factory = event_factory
        self._thread_factory = thread_factory
        self._event: Any = None
        self._thread: Any = None
        self._lifecycle_lock = threading.Lock()
        self._started = False
        self._enabled = False
        self._stopped = False

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.stop()
        return False

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._started or self._stopped:
                return
            self._started = True
            try:
                interactive = bool(self.stream.isatty())
            except Exception:
                interactive = False
            if not interactive:
                return
            self._enabled = True
            self._event = self._event_factory()
            self._thread = self._thread_factory(
                target=self._animate,
                daemon=True,
                name="pamu-working-spinner",
            )
            self._thread.start()

    def stop(self) -> None:
        with self._lifecycle_lock:
            if self._stopped:
                return
            self._stopped = True
            if not self._enabled:
                return
            event = self._event
            worker = self._thread
            event.set()

        if worker is threading.current_thread():
            return
        worker.join(1.0)
        self._clear()

    def _clear(self) -> None:
        with _TERMINAL_LOCK:
            if _visible_spinner is self:
                _erase_spinner()

    def _erase_line(self) -> None:
        self.stream.write(_CLEAR_LINE)
        self.stream.flush()

    def _animate(self) -> None:
        global _visible_spinner
        try:
            for frame in itertools.cycle(self.frames):
                if self._event.wait(self.interval):
                    return
                with _TERMINAL_LOCK:
                    if self._stopped:
                        return
                    if _model_line_open or _approval_open or _foreground_shell is not None:
                        continue
                    active = _background_status(mark_visible=True)
                    label = f'{frame} {self.label}'
                    if active:
                        label = f'{label} · {active}'
                    self.stream.write(f"\r{_fit_terminal_line(label)}\033[K")
                    self.stream.flush()
                    _visible_spinner = self
        finally:
            if self._stopped:
                self._clear()


def terminal_progress(_label: str) -> TerminalSpinner:
    return TerminalSpinner("Working")
