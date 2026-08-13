import threading

from agent_app.cli_progress import TerminalSpinner


class FakeStream:
    def __init__(self, *, tty):
        self.tty = tty
        self.writes = []
        self.flushes = 0

    def isatty(self):
        return self.tty

    def write(self, value):
        self.writes.append(value)

    def flush(self):
        self.flushes += 1


class FakeEvent:
    def __init__(self):
        self.set_calls = 0
        self.wait_calls = 0

    def set(self):
        self.set_calls += 1

    def wait(self, _timeout):
        self.wait_calls += 1
        return self.wait_calls > 1


class FakeThread:
    def __init__(self, created, *, target, daemon, name):
        self.target = target
        self.daemon = daemon
        self.name = name
        self.joined = []
        created.append(self)

    def start(self):
        self.target()

    def join(self, timeout):
        self.joined.append(timeout)


def test_tty_spinner_animates_then_clears_and_joins():
    stream = FakeStream(tty=True)
    created = []
    spinner = TerminalSpinner(
        "Working",
        stream=stream,
        frames=("A", "B"),
        interval=0.01,
        event_factory=FakeEvent,
        thread_factory=lambda **kwargs: FakeThread(created, **kwargs),
    )

    with spinner:
        pass

    assert "\rA Working" in "".join(stream.writes)
    assert stream.writes[-1] == "\r\033[2K"
    assert created[0].daemon is True
    assert created[0].name == "pamu-working-spinner"
    assert created[0].joined == [1.0]


def test_spinner_stop_is_idempotent():
    stream = FakeStream(tty=True)
    created = []
    spinner = TerminalSpinner(
        "Working",
        stream=stream,
        frames=("A",),
        interval=0.01,
        event_factory=FakeEvent,
        thread_factory=lambda **kwargs: FakeThread(created, **kwargs),
    )

    spinner.start()
    spinner.stop()
    spinner.stop()

    assert stream.writes.count("\r\033[2K") == 1
    assert created[0].joined == [1.0]


def test_non_tty_spinner_has_no_thread_or_output():
    stream = FakeStream(tty=False)
    created = []
    spinner = TerminalSpinner(
        "Working",
        stream=stream,
        event_factory=FakeEvent,
        thread_factory=lambda **kwargs: FakeThread(created, **kwargs),
    )

    with spinner:
        pass

    spinner.stop()

    assert created == []
    assert stream.writes == []
    assert stream.flushes == 0


def test_stop_waits_until_worker_has_started_before_joining():
    stream = FakeStream(tty=True)
    start_entered = threading.Event()
    allow_start = threading.Event()
    premature_join = threading.Event()
    errors = []

    class ControlledThread(FakeThread):
        def __init__(self, created, **kwargs):
            super().__init__(created, **kwargs)
            self.started = False

        def start(self):
            start_entered.set()
            allow_start.wait(1.0)
            self.started = True
            self.target()

        def join(self, timeout):
            if not self.started:
                premature_join.set()
                raise RuntimeError("cannot join thread before it is started")
            super().join(timeout)

    created = []
    spinner = TerminalSpinner(
        "Working",
        stream=stream,
        frames=("A",),
        interval=0.01,
        event_factory=FakeEvent,
        thread_factory=lambda **kwargs: ControlledThread(created, **kwargs),
    )

    def capture_errors(operation):
        try:
            operation()
        except Exception as exc:
            errors.append(exc)

    starter = threading.Thread(target=lambda: capture_errors(spinner.start))
    stopper = threading.Thread(target=lambda: capture_errors(spinner.stop))
    starter.start()
    assert start_entered.wait(1.0)
    stopper.start()
    raced = premature_join.wait(0.1)
    allow_start.set()
    starter.join(1.0)
    stopper.join(1.0)

    assert raced is False
    assert errors == []
    assert not starter.is_alive()
    assert not stopper.is_alive()
    assert created[0].joined == [1.0]
    assert stream.writes[-1] == "\r\033[2K"


def test_stop_before_start_permanently_prevents_worker_and_output():
    stream = FakeStream(tty=True)
    created = []
    spinner = TerminalSpinner(
        "Working",
        stream=stream,
        event_factory=FakeEvent,
        thread_factory=lambda **kwargs: FakeThread(created, **kwargs),
    )

    spinner.stop()
    spinner.start()
    spinner.start()
    spinner.stop()

    assert spinner._stopped is True
    assert created == []
    assert stream.writes == []
    assert stream.flushes == 0


def test_worker_originated_stop_exits_and_clears_exactly_once():
    stopped_inside_write = threading.Event()

    class SelfStoppingStream(FakeStream):
        spinner = None

        def write(self, value):
            super().write(value)
            if value != "\r\033[2K" and not stopped_inside_write.is_set():
                self.spinner.stop()
                stopped_inside_write.set()

    stream = SelfStoppingStream(tty=True)
    spinner = TerminalSpinner(
        "Working",
        stream=stream,
        frames=("A",),
        interval=0.001,
    )
    stream.spinner = spinner

    spinner.start()
    worker = spinner._thread
    stopped = stopped_inside_write.wait(1.0)
    worker.join(1.0)
    spinner.stop()

    assert stopped is True
    assert not worker.is_alive()
    assert stream.writes.count("\r\033[2K") == 1
    assert stream.writes[-1] == "\r\033[2K"


def test_isatty_error_disables_spinner_without_worker_or_output():
    class BrokenTTYStream(FakeStream):
        def isatty(self):
            raise OSError("terminal unavailable")

    stream = BrokenTTYStream(tty=True)
    created = []
    spinner = TerminalSpinner(
        "Working",
        stream=stream,
        event_factory=FakeEvent,
        thread_factory=lambda **kwargs: FakeThread(created, **kwargs),
    )

    with spinner:
        pass

    assert created == []
    assert stream.writes == []
    assert stream.flushes == 0
