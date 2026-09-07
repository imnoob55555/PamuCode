"""Shared exit gesture primitives for interactive CLI input."""

from enum import Enum, auto
from time import monotonic


class ExitAction(Enum):
    QUIT = auto()


class QuitRequested(Exception):
    """Raised by interactive UI when the user completes the exit gesture."""


class ExitGesture:
    def __init__(self, *, clock=monotonic, window=1.0):
        self.clock = clock
        self.window = window
        self.last = None

    def reset(self):
        self.last = None

    def press(self):
        now = self.clock()
        should_exit = (
            self.last is not None and 0 <= now - self.last <= self.window
        )
        self.last = None if should_exit else now
        return should_exit
