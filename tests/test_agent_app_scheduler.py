import json
from datetime import datetime

from agent_app.config import AppConfig
from agent_app.features.scheduler import (
    CronJob,
    SchedulerState,
    cron_scheduler_loop,
    load_durable_jobs,
    schedule_job,
    validate_cron,
)


def config_for(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_ID", "test-model")
    return AppConfig.from_env(tmp_path)


def test_scheduler_state_is_isolated(tmp_path, monkeypatch):
    config = config_for(tmp_path, monkeypatch)
    first = SchedulerState()
    second = SchedulerState()

    job = schedule_job(
        first,
        config,
        "*/5 * * * *",
        "run checks",
        recurring=True,
        durable=False,
    )

    assert isinstance(job, CronJob)
    assert job.id in first.jobs
    assert second.jobs == {}


def test_validate_cron_rejects_invalid_field():
    assert validate_cron("60 * * * *") == "minute: Value 60 out of bounds [0-59]"


class StopAfterFirstWait:
    def __init__(self):
        self.waits = 0

    def wait(self, _seconds):
        self.waits += 1
        return self.waits > 1


def test_scheduler_removes_one_shot_job_after_firing(tmp_path, monkeypatch):
    config = config_for(tmp_path, monkeypatch)
    state = SchedulerState()
    job = schedule_job(
        state,
        config,
        "* * * * *",
        "run once",
        recurring=False,
        durable=False,
    )

    cron_scheduler_loop(
        state,
        config,
        StopAfterFirstWait(),
        now=lambda: datetime(2026, 8, 12, 9, 30),
    )

    assert state.queue == [job]
    assert state.jobs == {}


def test_load_durable_jobs_loads_only_valid_jobs(tmp_path, monkeypatch):
    config = config_for(tmp_path, monkeypatch)
    config.scheduled_tasks_path.parent.mkdir(parents=True)
    config.scheduled_tasks_path.write_text(
        json.dumps([
            {
                "id": "valid",
                "cron": "0 9 * * 1",
                "prompt": "weekly review",
                "recurring": True,
                "durable": True,
            },
            {
                "id": "invalid",
                "cron": "60 * * * *",
                "prompt": "bad job",
                "recurring": True,
                "durable": True,
            },
        ]),
        encoding="utf-8",
    )
    state = SchedulerState()

    load_durable_jobs(state, config)

    assert list(state.jobs) == ["valid"]


def test_scheduler_fires_job_once_per_minute(tmp_path, monkeypatch):
    config = config_for(tmp_path, monkeypatch)
    state = SchedulerState()
    job = schedule_job(
        state,
        config,
        "* * * * *",
        "check once this minute",
        recurring=True,
        durable=False,
    )
    minute = datetime(2026, 8, 12, 9, 30)

    cron_scheduler_loop(state, config, StopAfterFirstWait(), now=lambda: minute)
    cron_scheduler_loop(state, config, StopAfterFirstWait(), now=lambda: minute)

    assert state.queue == [job]


def test_jobs_default_to_one_shot_session_jobs(tmp_path, monkeypatch):
    config = config_for(tmp_path, monkeypatch)
    state = SchedulerState()
    job = schedule_job(state, config, "0 9 * * *", "once")
    assert (job.recurring, job.durable, job.expires_at) == (False, False, None)
    assert not config.scheduled_tasks_path.exists()


def test_recurring_jobs_expire_after_seven_days(tmp_path, monkeypatch):
    from agent_app.features.scheduler import RECURRING_TTL_SECONDS
    config = config_for(tmp_path, monkeypatch)
    state = SchedulerState()
    start = datetime(2026, 8, 12, 9, 30)
    job = schedule_job(state, config, "* * * * *", "repeat", recurring=True, durable=True,
                       clock=lambda: start.timestamp())
    assert job.expires_at == start.timestamp() + RECURRING_TTL_SECONDS
    later = datetime.fromtimestamp(job.expires_at)
    cron_scheduler_loop(state, config, StopAfterFirstWait(), now=lambda: later)
    assert state.jobs == {} and state.queue == []
    assert json.loads(config.scheduled_tasks_path.read_text()) == []


def test_loading_drops_expired_jobs_and_bounds_legacy_recurring_jobs(tmp_path, monkeypatch):
    config = config_for(tmp_path, monkeypatch)
    config.scheduled_tasks_path.parent.mkdir(parents=True)
    now = 1_000_000.0
    config.scheduled_tasks_path.write_text(json.dumps([
        {"id": "legacy", "cron": "0 9 * * 1", "prompt": "weekly", "recurring": True, "durable": True},
        {"id": "expired", "cron": "0 9 * * 1", "prompt": "old", "recurring": True, "durable": True,
         "expires_at": now - 1},
    ]), encoding="utf-8")
    state = SchedulerState()
    assert load_durable_jobs(state, config, clock=lambda: now) == 1
    assert list(state.jobs) == ["legacy"] and state.jobs["legacy"].expires_at > now
    saved = json.loads(config.scheduled_tasks_path.read_text())
    assert [job["id"] for job in saved] == ["legacy"] and saved[0]["expires_at"] > now


def test_recurring_or_durable_jobs_need_approval(tmp_path, capsys):
    from types import SimpleNamespace
    from agent_app.tools.hooks import make_permission_hook
    prompts = []
    hook = make_permission_hook(tmp_path, lambda prompt: prompts.append(prompt) or "n")
    once = SimpleNamespace(name="schedule_cron", input={"cron": "0 9 * * *", "prompt": "check"})
    assert hook(once) is None and prompts == []
    repeating = SimpleNamespace(name="schedule_cron", input={"cron": "0 9 * * *", "prompt": "check", "recurring": True})
    assert hook(repeating) == "Permission denied by user"
    assert prompts == ["  Allow this scheduled task? [y/N] "]
    assert "Scheduled task needs approval" in capsys.readouterr().out


def test_cron_command_lists_and_cancels_jobs(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    from agent_app import cli
    config = config_for(tmp_path, monkeypatch)
    runtime = SimpleNamespace(scheduler=SchedulerState(), config=config, model=None)
    cli._handle_slash_command(runtime, "/cron")
    assert "没有定时任务" in capsys.readouterr().out
    first = schedule_job(runtime.scheduler, config, "0 9 * * *", "first task")
    second = schedule_job(runtime.scheduler, config, "*/5 * * * *", "second task", recurring=True)
    cli._handle_slash_command(runtime, "/cron")
    listing = capsys.readouterr().out
    assert f"{first.id}  '0 9 * * *'  one-shot, session  → first task" in listing
    assert f"{second.id}  '*/5 * * * *'  recurring, session, expires" in listing
    cli._handle_slash_command(runtime, f"/cron cancel {first.id}")
    assert f"Cancelled {first.id}" in capsys.readouterr().out
    cli._handle_slash_command(runtime, "/cron cancel all")
    assert runtime.scheduler.jobs == {}


def test_startup_reports_loaded_durable_jobs(tmp_path, monkeypatch, capsys):
    import threading
    from types import SimpleNamespace
    from agent_app import cli
    config = config_for(tmp_path, monkeypatch)
    config.scheduled_tasks_path.parent.mkdir(parents=True)
    config.scheduled_tasks_path.write_text(json.dumps([
        {"id": "kept", "cron": "0 9 * * 1", "prompt": "weekly", "recurring": False, "durable": True}]))
    runtime = SimpleNamespace(scheduler=SchedulerState(), config=config, stop_event=threading.Event())
    threads = cli.start_runtime_threads(runtime)
    cli.stop_runtime_threads(runtime, threads)
    assert "已加载 1 个持久定时任务，输入 /cron 查看或取消。" in capsys.readouterr().out
