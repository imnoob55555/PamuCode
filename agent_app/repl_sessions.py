"""Transactional REPL runtime replacement, separate from login return values."""
from contextlib import nullcontext
from dataclasses import dataclass

from .features.background import collect_background_results
from .features.sessions import checkpoint
from .features.session_store import SessionError
from .session_cli import select_session, show_restored_session


@dataclass(frozen=True)
class ResumeAction:
    args: tuple[str, ...]


class HandoffFatalError(SessionError):
    """Thread ownership is uncertain; the REPL must stop."""


def _alive(threads):
    return any(thread.is_alive() for thread in threads)


def _close(runtime):
    coordinator = getattr(runtime, 'sessions', None)
    if coordinator is not None:
        coordinator.close()
    adapter = getattr(getattr(runtime, 'model', None), 'llm', None)
    client = getattr(adapter, 'client', None)
    close = getattr(client, 'close', None)
    if callable(close):
        try:
            close()
        except Exception:
            pass  # Ownership has already transferred; cleanup cannot roll it back.


def _guard(runtime):
    background = getattr(runtime, 'background', None)
    if background is not None:
        with background.lock:
            running = [key for key, task in background.tasks.items() if task['status'] == 'running']
        if running:
            return '后台任务仍在运行：' + '、'.join(running[:5])
    team = getattr(runtime, 'team', None)
    if team is not None:
        with team.lock:
            if team.active:
                return '队友仍在运行：' + '、'.join(list(team.active)[:5])
    scheduler = getattr(runtime, 'scheduler', None)
    if scheduler is not None:
        # Caller holds scheduler.lock and pauses delivery in that same section.
        if scheduler.queue:
            return '有待处理的定时任务，请稍后重试。'
        transient = [job.id for job in scheduler.jobs.values() if not job.durable]
        if transient:
            return '请先完成或取消内存定时任务：' + '、'.join(transient[:5])
    bus = getattr(runtime, 'bus', None)
    if bus is not None:
        with bus.lock:
            path = bus.mailbox_path('lead')
            if path.exists() and path.stat().st_size:
                return '有待处理的团队消息，请先继续当前会话。'
    return None


def delete_current_session(runtime) -> str | None:
    """Delete the idle session and prevent further turns before REPL shutdown."""
    if not runtime.agent_lock.acquire(blocking=False):
        return '代理正在处理请求，请稍后重试。'
    try:
        scheduler = getattr(runtime, 'scheduler', None)
        with scheduler.lock if scheduler is not None else nullcontext():
            reason = _guard(runtime)
            if reason:
                return reason
            runtime.stop_event.set()
            runtime.sessions.delete_current()
        return None
    finally:
        runtime.agent_lock.release()


def resume_runtime(runtime, threads, action, *, create_runtime, start_threads, stop_threads, ui=None):
    """Return the new slot or the untouched old slot on a recoverable failure."""
    target = None
    lease = None
    locked = False
    scheduler = getattr(runtime, 'scheduler', None)
    paused = False
    committed = False
    shutdown_started = False
    try:
        coordinator = getattr(runtime, 'sessions', None)
        if coordinator is None:
            raise ValueError('当前运行时不支持会话恢复')
        selected = select_session(list(action.args), ui, exclude_id=coordinator.session_id,
                                  slash=True, workdir=runtime.config.workdir)
        if selected is None:
            return runtime, threads
        snapshot, lease = selected
        target = create_runtime(snapshot['selection'])
        if target is None:
            raise ValueError('无法构建目标会话运行时，保留当前会话。')
        if target is runtime or getattr(target, 'sessions', None) is None:
            raise ValueError('目标运行时不支持独立会话恢复')
        target.sessions.restore(target, snapshot, lease)
        locked = runtime.agent_lock.acquire(blocking=False)
        if not locked:
            raise ValueError('代理正在处理请求，请稍后重试。')
        with scheduler.lock if scheduler is not None else nullcontext():
            reason = _guard(runtime)
            if reason:
                raise ValueError(reason)
            if scheduler is not None:
                scheduler.paused = True
                paused = True
                dedup = dict(scheduler.last_fired)
        background = getattr(runtime, 'background', None)
        if background is not None:
            notifications = collect_background_results(background)
            if notifications:
                from .core.context import append_user_text_blocks
                append_user_text_blocks(runtime.session.history, notifications)
        # A storage failure is fatal, just like any ordinary REPL checkpoint.
        try:
            checkpoint(runtime)
        except SessionError as error:
            raise HandoffFatalError(str(error)) from error
        shutdown_started = True
        try:
            stop_threads(runtime, threads)
        except BaseException as error:
            raise HandoffFatalError('无法确认旧会话线程已停止，请重新启动。') from error
        if _alive(threads):
            raise HandoffFatalError('旧会话线程未停止，请重新启动。')
        target_scheduler = getattr(target, 'scheduler', None)
        if scheduler is not None and target_scheduler is not None:
            with target_scheduler.lock:
                target_scheduler.last_fired.update(dedup)
        try:
            new_threads = start_threads(target)
        except BaseException as error:
            # Default starter stops and joins its partial startup before raising.
            # Injected starters may attach handles to the error for verification.
            partial = getattr(error, 'threads', [])
            try:
                stop_threads(target, partial)
            except BaseException as cleanup_error:
                raise HandoffFatalError('目标线程清理失败，请重新启动。') from cleanup_error
            if _alive(partial):
                raise HandoffFatalError('目标线程仍在运行，请重新启动。') from error
            if isinstance(error, (KeyboardInterrupt, SystemExit)) and not hasattr(error, 'threads'):
                raise HandoffFatalError('目标启动被中断，无法确认部分线程已清理，请重新启动。') from error
            event = getattr(runtime, 'stop_event', None)
            if event is not None:
                event.clear()
            if scheduler is not None:
                with scheduler.lock:
                    scheduler.paused = False
                    paused = False
            try:
                threads = start_threads(runtime)
            except BaseException as restart_error:
                raise HandoffFatalError('原会话线程恢复失败，请重新启动。') from restart_error
            shutdown_started = False
            print('目标会话启动失败，已恢复原会话。')
            return runtime, threads
        committed = True
        _close(runtime)
        show_restored_session(snapshot, target)
        return target, new_threads
    except (HandoffFatalError, SessionError) as error:
        # Selection/restore errors are recoverable; failed current saves are not.
        if isinstance(error, HandoffFatalError) or getattr(getattr(runtime, 'sessions', None), 'failed', False):
            raise
        print(f'会话错误：{error}')
        return runtime, threads
    except (EOFError, KeyboardInterrupt) as error:
        if committed:
            return target, new_threads
        if shutdown_started:
            raise HandoffFatalError('会话切换被中断，无法确认线程所有权，请重新启动。') from error
        return runtime, threads
    except Exception as error:
        if committed:
            # Presentation/old-client cleanup cannot put a stopped, closed slot
            # back into service after the target has assumed ownership.
            return target, new_threads
        if shutdown_started:
            raise HandoffFatalError('会话切换失败，无法确认线程所有权，请重新启动。') from error
        print(f'无法恢复会话：{error}')
        return runtime, threads
    finally:
        if paused and not committed:
            with scheduler.lock:
                scheduler.paused = False
        if locked:
            runtime.agent_lock.release()
        if not committed:
            if target is not None and target is not runtime:
                _close(target)
            if lease is not None:
                lease.close()
