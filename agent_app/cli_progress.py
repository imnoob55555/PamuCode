"""Terminal progress feedback for interactive model requests."""

from __future__ import annotations

import itertools
import sys
import threading


DEFAULT_FRAMES = tuple("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏")


class TerminalSpinner:
    """Animate a status label on an interactive terminal until stopped."""

    def __init__(
        self,
        label: str,
        *,
        stream=None,
        frames=DEFAULT_FRAMES,
        interval: float = 0.08,
        event_factory=threading.Event,
        thread_factory=threading.Thread,
    ) -> None:
        self.label = label
        self.stream = sys.stderr if stream is None else stream
        self.frames = tuple(frames)
        self.interval = interval
        self._event_factory = event_factory
        self._thread_factory = thread_factory
        self._event = None
        self._thread = None
        self._lifecycle_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._started = False
        self._enabled = False
        self._stopped = False
        self._cleared = False

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.stop()
        return False

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._started:
                return
            self._started = True
            if not self.stream.isatty():
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
            if not self._enabled or self._stopped:
                return
            self._stopped = True
            event = self._event
            worker = self._thread
            event.set()

        if worker is not threading.current_thread():
            worker.join(1.0)

        with self._write_lock:
            if self._cleared:
                return
            self.stream.write("\r\033[2K")
            self.stream.flush()
            self._cleared = True

    def _animate(self) -> None:
        for frame in itertools.cycle(self.frames):
            if self._event.wait(self.interval):
                return
            with self._write_lock:
                if self._stopped:
                    return
                self.stream.write(f"\r{frame} {self.label}")
                self.stream.flush()


def terminal_progress(label: str) -> TerminalSpinner:
    return TerminalSpinner(label)
