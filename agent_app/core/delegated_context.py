"""Independent context state for synchronous and threaded delegated work."""
from types import SimpleNamespace
from uuid import uuid4

from ..adapters.common import null_progress
from .context_budget import prepare_budget, record_usage
from .recovery import RecoveryState, with_retry


class DelegatedContext:
    def __init__(self, config, llm, model, *, protocol='anthropic_messages', origin='', adapter=None,
                 fallback_model=None, progress=null_progress):
        self._recovery = RecoveryState(model, fallback_model)
        self._progress = progress
        self.runtime = SimpleNamespace(
            config=config, llm=adapter or SimpleNamespace(create=llm), sessions=None,
            model=SimpleNamespace(primary_model=model, protocol=protocol, base_url=origin, llm=adapter),
            session=SimpleNamespace(history=[], context={}, context_state={'archive_namespace': str(uuid4())}),
        )

    def retrieval_tools(self):
        """Bind evidence tools to this delegate's private archive namespace."""
        from .context_manager import register_history_tools
        from ..tools.registry import ToolRegistry
        from ..features.session_store import SessionError
        if not hasattr(self.runtime, 'tools'):
            self.runtime.tools = ToolRegistry()
            register_history_tools(self.runtime)
        schemas, handlers = self.runtime.tools.snapshot()
        def guarded(handler):
            def invoke(**kwargs):
                try:
                    return handler(**kwargs)
                except (SessionError, TypeError, ValueError) as error:
                    return f'Error: {error}'
            return invoke
        return schemas, {name: guarded(handler) for name, handler in handlers.items()}

    def request(self, llm, **kwargs):
        """Prepare, retry and record one request against this delegate's selection.

        Keep the caller's history through compaction; only copy it at the
        adapter seam. Plain callable injection retains its model keyword
        convention, while a captured adapter always uses the recovery model.
        """
        config = self.runtime.config

        def attempt():
            active = self._recovery.current_model
            report = self._prepare(kwargs.get('system'), kwargs['messages'],
                                   kwargs.get('tools'), kwargs['max_tokens'], active)
            create = llm
            adapter = self.runtime.model.llm
            if adapter is not None and hasattr(adapter, 'with_context_window'):
                create = adapter.with_context_window(report.budget.window).create
            request = {**kwargs, 'messages': list(kwargs['messages']),
                       'max_tokens': report.budget.output_tokens}
            if adapter is not None or 'model' in request:
                request['model'] = active
            with self._progress("Working"):
                response = create(**request)
            record_usage(self.runtime, response, model=active, estimated_input=report.input_tokens)
            return response

        return with_retry(
            attempt, self._recovery,
            max_transient_retries=config.max_transient_retries,
            max_consecutive_529=config.max_consecutive_529,
            base_delay_ms=config.base_delay_ms,
            progress=self._progress,
        )

    def _prepare(self, system, messages, tools, requested_max_tokens, model):
        from .context_manager import compact, normalize_results
        runtime = self.runtime
        runtime.session.history = messages
        report = prepare_budget(runtime, system, messages, tools, requested_max_tokens, model=model)
        normalize_results(runtime, messages, min(4000, report.budget.input_limit * 15 // 100))
        report = prepare_budget(runtime, system, messages, tools, requested_max_tokens, model=model)
        if report.input_tokens >= report.budget.trigger:
            compact(runtime, system=system, tools=tools, model=model)
            report = prepare_budget(runtime, system, messages, tools, requested_max_tokens, model=model)
        if report.remaining < 0:
            raise ValueError('子代理必要上下文超过输入预算，请调整上下文窗口或缩减任务')
        return report
