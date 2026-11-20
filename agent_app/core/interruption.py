"""User interruption shared by model streams, commands, and the CLI."""
from contextlib import contextmanager
from contextvars import ContextVar
import signal
import threading


class ResponseInterrupted(KeyboardInterrupt):
    def __init__(self, partial_text):
        self.partial_text = partial_text
        super().__init__('用户中断模型响应')


class CommandInterrupted(KeyboardInterrupt):
    def __init__(self, output='', *, started=True):
        super().__init__(
            ('命令已中断，可能已部分执行；请先检查实际状态。' if started else
             '用户已取消，此命令未执行。')
            + (f'\n已捕获的输出：\n{output}' if output else '')
        )


_command_cancellation = ContextVar('command_cancellation', default=None)


@contextmanager
def command_cancellation(event):
    token = _command_cancellation.set(event)
    try:
        yield
    finally:
        _command_cancellation.reset(token)


def check_command_cancelled(*, started=False):
    event = _command_cancellation.get()
    if event is not None and event.is_set():
        raise CommandInterrupted(started=started)


@contextmanager
def defer_sigint():
    """Deliver Ctrl+C after a newly spawned process has an owner."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    pending = None

    def remember(number, frame):
        nonlocal pending
        pending = (number, frame)

    previous = signal.getsignal(signal.SIGINT)
    if not callable(previous):
        yield
        return
    signal.signal(signal.SIGINT, remember)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)
        if pending is not None:
            previous(*pending)


@contextmanager
def protect_interrupt_cleanup():
    """A second Ctrl+C must not tear down process reaping or durable repair."""
    main = threading.current_thread() is threading.main_thread()
    previous = signal.signal(signal.SIGINT, signal.SIG_IGN) if main else None
    try:
        yield
    finally:
        if main:
            signal.signal(signal.SIGINT, previous)
