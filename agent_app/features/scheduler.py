import json
import random
import threading
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, cast

from .. import theme
from ..config import AppConfig
from ..logging_setup import get_logger

LOGGER = get_logger("pamu.scheduler")

SCHEDULER_TOOL_SCHEMAS = [
    {"name": "schedule_cron",
     "description": "Schedule a cron job. cron is 5-field: min hour dom month dow.",
     "input_schema": {"type": "object",
                      "properties": {
                          "cron": {"type": "string",
                                   "description": "5-field cron expression"},
                          "prompt": {"type": "string",
                                     "description": "Message to inject when fired"},
                          "recurring": {"type": "boolean",
                                        "description": "True=recurring (expires after 7 days), "
                                                       "False=one-shot. Default false; true needs user approval."},
                          "durable": {"type": "boolean",
                                      "description": "True=persist across restarts. "
                                                     "Default false; true needs user approval."}},
                      "required": ["cron", "prompt"]}},
    {"name": "list_crons",
     "description": "List all registered cron jobs.",
     "input_schema": {"type": "object", "properties": {},
                      "required": []}},
    {"name": "cancel_cron",
     "description": "Cancel a cron job by ID.",
     "input_schema": {"type": "object",
                      "properties": {"job_id": {"type": "string"}},
                      "required": ["job_id"]}},
]


def register_scheduler_tools(registry, scheduler_state: Any, config) -> None:
    """Register cron tools using the supplied scheduler handlers."""
    if isinstance(scheduler_state, Mapping):
        handlers = scheduler_state
    else:
        def current_state() -> "SchedulerState":
            return cast(
                SchedulerState,
                scheduler_state() if callable(scheduler_state) else scheduler_state,
            )

        def run_schedule(cron, prompt, recurring=False, durable=False):
            result = schedule_job(
                current_state(), config, cron, prompt, recurring, durable
            )
            if isinstance(result, str):
                return f"Error: {result}"
            return f"Scheduled {result.id}: '{cron}' → '{prompt}' [{describe_job(result)}]"

        def run_list():
            jobs = list_jobs(current_state())
            if not jobs:
                return "No cron jobs. Use schedule_cron to add one."
            lines = []
            for job in jobs:
                lines.append(f"  {job.id}: '{job.cron}' → {job.prompt[:40]} [{describe_job(job)}]")
            return "\n".join(lines)

        handlers = {
            "schedule_cron": run_schedule,
            "list_crons": run_list,
            "cancel_cron": lambda job_id: cancel_job(
                current_state(), config, job_id
            ),
        }
    for schema in SCHEDULER_TOOL_SCHEMAS:
        registry.register(schema, handlers[schema["name"]])


RECURRING_TTL_SECONDS = 7 * 24 * 3600


@dataclass
class CronJob:
    id: str
    cron: str
    prompt: str
    recurring: bool
    durable: bool
    expires_at: float | None = None


def is_expired(job: CronJob, now: float) -> bool:
    return job.expires_at is not None and now >= job.expires_at


def describe_job(job: CronJob) -> str:
    parts = ["recurring" if job.recurring else "one-shot", "durable" if job.durable else "session"]
    if job.expires_at is not None:
        parts.append("expires " + datetime.fromtimestamp(job.expires_at).strftime("%Y-%m-%d %H:%M"))
    return ", ".join(parts)


@dataclass(slots=True)
class SchedulerState:
    paused: bool = False
    jobs: dict[str, CronJob] = field(default_factory=dict)
    queue: list[CronJob] = field(default_factory=list)
    last_fired: dict[str, str] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)


def _parse_int(text: str) -> int | None:
    """Parse ``text`` defensively; oversized digit strings may overflow."""
    try:
        return int(text)
    except ValueError:
        return None


def _cron_field_matches(field: str, value: int) -> bool:
    if field == "*":
        return True
    if field.startswith("*/"):
        step = _parse_int(field[2:])
        return step is not None and step > 0 and value % step == 0
    if "," in field:
        return any(_cron_field_matches(part.strip(), value) for part in field.split(","))
    if "-" in field:
        low, high = field.split("-", 1)
        low_value, high_value = _parse_int(low), _parse_int(high)
        return (
            low_value is not None
            and high_value is not None
            and low_value <= value <= high_value
        )
    parsed = _parse_int(field)
    return parsed is not None and value == parsed


def cron_matches(cron_expr: str, dt: datetime) -> bool:
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        return False
    minute, hour, day_of_month, month, day_of_week = fields
    day_of_week_value = (dt.weekday() + 1) % 7

    minute_matches = _cron_field_matches(minute, dt.minute)
    hour_matches = _cron_field_matches(hour, dt.hour)
    day_of_month_matches = _cron_field_matches(day_of_month, dt.day)
    month_matches = _cron_field_matches(month, dt.month)
    day_of_week_matches = _cron_field_matches(day_of_week, day_of_week_value)

    if not (minute_matches and hour_matches and month_matches):
        return False
    day_of_month_unconstrained = day_of_month == "*"
    day_of_week_unconstrained = day_of_week == "*"
    if day_of_month_unconstrained and day_of_week_unconstrained:
        return True
    if day_of_month_unconstrained:
        return day_of_week_matches
    if day_of_week_unconstrained:
        return day_of_month_matches
    return day_of_month_matches or day_of_week_matches


def _validate_cron_field(field: str, low: int, high: int) -> str | None:
    if field == "*":
        return None
    if field.startswith("*/"):
        step_string = field[2:]
        if not step_string.isdigit():
            return f"Invalid step: {field}"
        step = _parse_int(step_string)
        if step is None:
            return f"Invalid step: {field}"
        if step <= 0:
            return f"Step must be > 0: {field}"
        return None
    if "," in field:
        for part in field.split(","):
            error = _validate_cron_field(part.strip(), low, high)
            if error:
                return error
        return None
    if "-" in field:
        parts = field.split("-", 1)
        if not parts[0].isdigit() or not parts[1].isdigit():
            return f"Invalid range: {field}"
        start, end = _parse_int(parts[0]), _parse_int(parts[1])
        if start is None or end is None:
            return f"Invalid range: {field}"
        if start < low or start > high or end < low or end > high:
            return f"Range {field} out of bounds [{low}-{high}]"
        if start > end:
            return f"Range start > end: {field}"
        return None
    if not field.isdigit():
        return f"Invalid field: {field}"
    value = _parse_int(field)
    if value is None:
        return f"Invalid field: {field}"
    if value < low or value > high:
        return f"Value {value} out of bounds [{low}-{high}]"
    return None


def validate_cron(cron_expr: str) -> str | None:
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        return f"Expected 5 fields, got {len(fields)}"
    bounds = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
    names = ["minute", "hour", "day-of-month", "month", "day-of-week"]
    for cron_field, (low, high), name in zip(fields, bounds, names, strict=True):
        error = _validate_cron_field(cron_field, low, high)
        if error:
            return f"{name}: {error}"
    return None


def save_durable_jobs(state: SchedulerState, config: AppConfig):
    with state.lock:
        durable = [asdict(job) for job in state.jobs.values() if job.durable]
        temp_path = config.scheduled_tasks_path.with_suffix(".tmp")
        temp_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path.write_text(
            json.dumps(durable, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    temp_path.replace(config.scheduled_tasks_path)


def load_durable_jobs(state: SchedulerState, config: AppConfig, clock=time.time) -> int:
    """Load persisted jobs, dropping expired ones; return how many were loaded.

    Recurring jobs saved before expiry existed get a fresh expiry window.
    """
    if not config.scheduled_tasks_path.exists():
        return 0
    now = clock()
    try:
        jobs = json.loads(config.scheduled_tasks_path.read_text())
        loaded, changed = [], False
        with state.lock:
            for serialized in jobs:
                job = CronJob(**serialized)
                error = validate_cron(job.cron)
                if error:
                    print("  " + theme.paint(f"[cron] skipping invalid job {job.id}: {error}", "error"))
                    continue
                if job.recurring and job.expires_at is None:
                    job.expires_at, changed = now + RECURRING_TTL_SECONDS, True
                if is_expired(job, now):
                    changed = True
                    continue
                state.jobs[job.id] = job
                loaded.append(job.id)
        if changed:
            save_durable_jobs(state, config)
        if loaded:
            LOGGER.info("loaded %d durable jobs", len(loaded))
        return len(loaded)
    except Exception:
        LOGGER.warning("ignored malformed durable cron file", exc_info=True)
        return 0


def schedule_job(
    state: SchedulerState,
    config: AppConfig,
    cron: str,
    prompt: str,
    recurring: bool = False,
    durable: bool = False,
    clock=time.time,
) -> CronJob | str:
    error = validate_cron(cron)
    if error:
        return error
    job = CronJob(
        id=f"cron_{random.randint(0, 999999):06d}",  # noqa: S311 - job id, not a secret
        cron=cron,
        prompt=prompt,
        recurring=recurring,
        durable=durable,
        expires_at=clock() + RECURRING_TTL_SECONDS if recurring else None,
    )
    with state.lock:
        state.jobs[job.id] = job
    if durable:
        save_durable_jobs(state, config)
    LOGGER.info("registered cron job %s (%s)", job.id, cron)
    return job


def cancel_job(state: SchedulerState, config: AppConfig, job_id: str) -> str:
    with state.lock:
        job = state.jobs.pop(job_id, None)
    if not job:
        return f"Job {job_id} not found"
    if job.durable:
        save_durable_jobs(state, config)
    LOGGER.info("cancelled cron job %s", job_id)
    return f"Cancelled {job_id}"


def list_jobs(state: SchedulerState) -> list[CronJob]:
    with state.lock:
        return list(state.jobs.values())


def cron_scheduler_loop(state: SchedulerState, config: AppConfig, stop_event, now=datetime.now):
    while not stop_event.wait(1):
        current_time = now()
        minute_marker = current_time.strftime("%Y-%m-%d %H:%M")
        durable_changed = False
        with state.lock:
            if state.paused:
                continue
            for job in list(state.jobs.values()):
                if is_expired(job, current_time.timestamp()):
                    state.jobs.pop(job.id, None)
                    state.last_fired.pop(job.id, None)
                    durable_changed = durable_changed or job.durable
                    LOGGER.info("expired cron job %s", job.id)
                    continue
                try:
                    if cron_matches(job.cron, current_time):
                        if state.last_fired.get(job.id) != minute_marker:
                            state.queue.append(job)
                            state.last_fired[job.id] = minute_marker
                            LOGGER.info("fired cron job %s", job.id)
                        if not job.recurring:
                            state.jobs.pop(job.id, None)
                            state.last_fired.pop(job.id, None)
                            durable_changed = durable_changed or job.durable
                except Exception as error:
                    print("  " + theme.paint(f"[cron error] {job.id}: {error}", "error"))
        if durable_changed:
            save_durable_jobs(state, config)


def consume_cron_queue(state: SchedulerState) -> list[CronJob]:
    with state.lock:
        fired = list(state.queue)
        state.queue.clear()
    return fired


def queued_jobs(state: SchedulerState) -> list[CronJob]:
    with state.lock:
        return list(state.queue)


def has_cron_queue(state: SchedulerState) -> bool:
    with state.lock:
        return bool(state.queue)
