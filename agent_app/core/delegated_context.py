"""Independent context state for synchronous and threaded delegated work."""
from types import SimpleNamespace
from uuid import uuid4

from .context_budget import prepare_budget, record_usage


class DelegatedContext:
    def __init__(self, config, llm, model, *, protocol='anthropic_messages', origin='', adapter=None):
        self.llm = llm
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

    def prepare(self, system, messages, tools, requested_max_tokens, model=None):
        from .context_manager import compact, normalize_results
        runtime = self.runtime
        runtime.session.history = messages
        active = model or runtime.model.primary_model
        report = prepare_budget(runtime, system, messages, tools, requested_max_tokens, model=active)
        if hasattr(runtime.llm, "with_context_window"):
            runtime.llm = runtime.llm.with_context_window(report.budget.window)
        normalize_results(runtime, messages, min(4000, report.budget.input_limit * 15 // 100))
        report = prepare_budget(runtime, system, messages, tools, requested_max_tokens, model=active)
        if report.input_tokens >= report.budget.trigger:
            compact(runtime, system=system, tools=tools, model=active)
            report = prepare_budget(runtime, system, runtime.session.history, tools, requested_max_tokens, model=active)
        if report.remaining < 0:
            raise ValueError('子代理必要上下文超过输入预算，请调整上下文窗口或缩减任务')
        messages[:] = runtime.session.history
        return report

    def record(self, response, report, model=None):
        return record_usage(self.runtime, response, model, estimated_input=report.input_tokens)
