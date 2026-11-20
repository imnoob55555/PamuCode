"""Bounded noninteractive execution using the production runtime and agent loop."""
from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from dataclasses import replace

from ..adapters.common import ModelResponse, TextBlock, ToolUseBlock, value
from ..core.context_budget import extract_usage
from ..security import ExecutionPolicy, ShellExecutor, SecurityDenied


class ExecutionStop(BaseException):
    def __init__(self, status, reason):
        self.status, self.reason = status, reason


class Ledger:
    def __init__(self, output, calls, tools, tokens, timeout, protocol="anthropic_messages"):
        self.protocol = protocol
        self.lock = threading.RLock()
        self.output = output
        self.deadline = time.monotonic() + timeout
        self.limits = calls, tools, tokens
        self.model_calls = self.tool_calls = self.input_tokens = self.output_tokens = 0
        self.known = True
        self.completed = False
        self.final_answer = ''
        self.secrets = [v for k,v in os.environ.items() if v and any(word in k.upper() for word in ('API_KEY','AUTH_TOKEN','SECRET','PASSWORD'))]

    def event(self, kind, **data):
        with self.lock, (self.output/'events.jsonl').open('a') as f:
            f.write(json.dumps(self.redact_values({'type':kind, 'time':time.time(), **data}), ensure_ascii=False)+'\n')

    def redact(self, text):
        for secret in self.secrets:
            text = text.replace(secret, '[REDACTED]')
        return text

    def redact_values(self, data):
        if isinstance(data, str):
            return self.redact(data)
        if isinstance(data, dict):
            return {self.redact(key) if isinstance(key, str) else key: self.redact_values(item) for key, item in data.items()}
        if isinstance(data, list):
            return [self.redact_values(item) for item in data]
        return data

    def check(self):
        if time.monotonic() >= self.deadline:
            raise ExecutionStop('budget_exceeded', 'timeout')

    def tool(self, block):
        with self.lock:
            self.check()
            if self.tool_calls >= self.limits[1]:
                raise ExecutionStop('budget_exceeded', 'max_tool_calls')
            self.tool_calls += 1
            self.event('tool.started', name=block.name, tool_call_id=block.id, input=block.input)


class ReplayAdapter:
    def __init__(self, path):
        self.responses = iter(json.loads(Path(path).read_text()))

    def create(self, **kwargs):
        item = next(self.responses)
        blocks = []
        for block in item['content']:
            if block['type'] == 'text':
                blocks.append(TextBlock(block['text']))
            elif block['type'] == 'tool_use':
                blocks.append(ToolUseBlock(block['id'], block['name'], block['input']))
            else:
                raise ValueError('unsupported replay block')
        response = ModelResponse(blocks, item['stop_reason'], item.get('usage'))
        response.usage = extract_usage(response, 'anthropic_messages')
        return response


class TrackedAdapter:
    def __init__(self, adapter, ledger):
        self.adapter, self.ledger = adapter, ledger

    def __getattr__(self, name):
        return getattr(self.adapter, name)

    def with_context_window(self, window):
        adapter = self.adapter.with_context_window(window) if hasattr(self.adapter, 'with_context_window') else self.adapter
        return TrackedAdapter(adapter, self.ledger)

    def _request(self, streaming, **kwargs):
        ledger = self.ledger
        with ledger.lock:
            ledger.check()
            if ledger.model_calls >= ledger.limits[0]:
                raise ExecutionStop('budget_exceeded', 'max_model_calls')
            if ledger.input_tokens + ledger.output_tokens >= ledger.limits[2]:
                raise ExecutionStop('budget_exceeded', 'max_tokens')
            kwargs['max_tokens'] = min(kwargs.get('max_tokens', ledger.limits[2]), max(1, ledger.limits[2]-ledger.input_tokens-ledger.output_tokens))
            ledger.model_calls += 1
            ledger.completed = False
            ledger.event('model_request', model=kwargs.get('model'), call=ledger.model_calls)
        try:
            method = getattr(self.adapter, 'create_streaming', self.adapter.create) if streaming else self.adapter.create
            response = method(**kwargs)
        except ExecutionStop:
            ledger.known = False
            raise
        except Exception as exc:
            ledger.known = False
            ledger.event('model_error', error_type=type(exc).__name__)
            raise ExecutionStop('error', 'model_error') from exc
        with ledger.lock:
            usage = extract_usage(response, ledger.protocol)
            incoming, outgoing = value(usage, 'input_tokens'), value(usage, 'output_tokens')
            if not isinstance(incoming, int) or not isinstance(outgoing, int):
                ledger.known = False
            else:
                ledger.input_tokens += incoming
                ledger.output_tokens += outgoing
            ledger.event('model.completed', response={'content':[{'type':value(b,'type'), **({'text':value(b,'text')} if value(b,'type')=='text' else {'id':value(b,'id'),'name':value(b,'name'),'input':value(b,'input')})} for b in response.content], 'stop_reason':response.stop_reason}, usage={'input_tokens':incoming,'output_tokens':outgoing,'cache_read_tokens':value(usage,'cache_read_tokens'),'cache_write_tokens':value(usage,'cache_write_tokens')})
            ledger.check()
            if ledger.input_tokens + ledger.output_tokens > ledger.limits[2]:
                raise ExecutionStop('budget_exceeded', 'max_tokens')
            if streaming and response.stop_reason in ('end_turn', 'stop', 'stop_sequence') and not any(value(b,'type')=='tool_use' for b in response.content):
                ledger.completed = True
                ledger.final_answer = '\n'.join(value(b,'text','') for b in response.content if value(b,'type')=='text')
        return response

    def create(self, **kwargs):
        return self._request(False, **kwargs)

    def create_streaming(self, **kwargs):
        return self._request(True, **kwargs)


DEFAULT_TOOLS = frozenset({'bash','read_file','write_file','edit_file','glob'})


def execute(*, task, workdir, output, model, protocol, max_model_calls=30, max_tool_calls=100, max_tokens=100000, timeout=300, replay=None, allowed_tools=None, sandbox=None):
    from ..bootstrap import build_runtime, INSTALL_ROOT
    from ..config import AppConfig
    from ..core.loop import run_agent_loop
    from ..tools.hooks import HookRegistry
    if any(type(v) is not int or v <= 0 for v in (max_model_calls, max_tool_calls, max_tokens)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('budgets must be positive')
    if protocol not in ('anthropic_messages', 'openai_chat', 'openai_responses'):
        raise ValueError('unsupported protocol')
    mode = sandbox if sandbox is not None else os.environ.get('PAMU_SANDBOX', 'off')
    policy = ExecutionPolicy(Path(workdir), INSTALL_ROOT, mode, tuple(path for path in (output, replay) if path is not None))
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any((output/name).exists() for name in ('result.json','events.jsonl')):
        raise ValueError('output already contains execution evidence')
    (output/'events.jsonl').write_text('')
    ledger = Ledger(output, max_model_calls, max_tool_calls, max_tokens, timeout, protocol)
    status, reason = 'error', 'runtime_incomplete'
    allowed = DEFAULT_TOOLS if allowed_tools is None else frozenset(allowed_tools)
    saved_signals = {}
    old_timer = None
    if threading.current_thread() is threading.main_thread():
        def stop_signal(number, frame):
            raise ExecutionStop('budget_exceeded' if number == signal.SIGALRM else 'cancelled', 'timeout' if number == signal.SIGALRM else 'signal')
        for number in (signal.SIGTERM, signal.SIGINT, signal.SIGALRM):
            saved_signals[number] = signal.signal(number, stop_signal)
        old_timer = signal.setitimer(signal.ITIMER_REAL, timeout)
    try:
        if not allowed <= DEFAULT_TOOLS:
            raise ValueError('unsupported headless tools')
        if replay:
            adapter = ReplayAdapter(replay)
            provider, base_url = 'replay', ''
        else:
            from ..provider_config import resolve_provider_config
            from ..adapters.providers import build_client, build_adapter
            environment = {**os.environ, 'MODEL_ID':model}
            if protocol in ('openai_chat','openai_responses'):
                environment.setdefault('PAMU_PROVIDER','openai-compatible')
                environment['OPENAI_COMPATIBLE_PROTOCOL'] = protocol
            resolved = resolve_provider_config(environment, {})
            if resolved.protocol != protocol:
                raise ValueError('selected provider protocol does not match --protocol')
            client = build_client(resolved.spec, resolved.api_key, base_url=resolved.base_url, auth_token=resolved.auth_token)
            adapter = build_adapter(resolved.spec, client, base_url=resolved.base_url, keyless=not resolved.api_key)
            provider, base_url = resolved.spec.name, resolved.base_url
        tracked = TrackedAdapter(adapter, ledger)
        config = AppConfig.from_env(INSTALL_ROOT, Path(workdir), {'MODEL_ID':model, 'PAMU_SANDBOX':mode})
        config = replace(config, headless=True, max_transient_retries=1, max_consecutive_529=0, default_max_tokens=min(8000,max_tokens), escalated_max_tokens=min(8000,max_tokens))
        class RedactedWriter:
            def __init__(self):
                self.pending = ''
                self.lookahead = max([len(secret) for secret in ledger.secrets] or [1])

            def write(self, text):
                self.pending += text
                self.drain(False)
                return len(text)

            def drain(self, final):
                output = []
                consumed = 0
                while consumed < len(self.pending) and (final or len(self.pending)-consumed >= self.lookahead):
                    matched = next((secret for secret in ledger.secrets if self.pending.startswith(secret, consumed)), None)
                    if matched:
                        output.append('[REDACTED]')
                        consumed += len(matched)
                    else:
                        output.append(self.pending[consumed])
                        consumed += 1
                self.pending = self.pending[consumed:]
                if output:
                    original_stderr.write(''.join(output))

            def flush(self):
                original_stderr.flush()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.drain(True)
                self.flush()

        original_stderr = sys.stderr
        with RedactedWriter() as writer, contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
            runtime = build_runtime(config, None, adapter=tracked, protocol=protocol, provider=provider, base_url=base_url, thinking_level='off', execution_policy=policy, shell_deadline=ledger.deadline)
            hooks = HookRegistry()
            def pre(block):
                ledger.tool(block)
                if block.name in {'read_file', 'write_file', 'edit_file'}:
                    from ..tools.builtin import safe_path
                    try:
                        safe_path(config.workdir, block.input.get('path', ''), policy=policy, write=block.name != 'read_file')
                    except (ValueError, PermissionError) as exc:
                        ledger.event('policy_denied', name=block.name)
                        raise ExecutionStop('policy_denied', 'protected_path') from exc
                if block.name not in allowed or block.input.get('run_in_background'):
                    ledger.event('policy_denied', name=block.name)
                    raise ExecutionStop('policy_denied', 'tool_not_permitted')
            hooks.register('PreToolUse', pre)
            hooks.register('PostToolUse', lambda block, result: ledger.event('tool.completed', tool_call_id=block.id, output=str(result)))
            runtime.hooks = hooks
            with runtime.tools._lock:
                runtime.tools._tools = {k:v for k,v in runtime.tools._tools.items() if k in allowed}
                runtime.tools._handlers = {k:v for k,v in runtime.tools._handlers.items() if k in allowed}
            runtime.session.history.append({'role':'user','content':task})
            loop_stop = run_agent_loop(runtime)
        if ledger.completed:
            status, reason = 'completed', 'end_turn'
        elif loop_stop == 'repeated_tool_calls':
            status, reason = 'budget_exceeded', loop_stop
    except subprocess.TimeoutExpired:
        status, reason = 'budget_exceeded', 'timeout'
    except SecurityDenied:
        status, reason = 'policy_denied', 'sandbox_unavailable'
    except ExecutionStop as stop:
        status, reason = stop.status, stop.reason
    except Exception as exc:
        ledger.event('runtime_error', error_type=type(exc).__name__)
        status, reason = 'error', 'runtime_error'
    finally:
        if old_timer is not None:
            signal.setitimer(signal.ITIMER_REAL, 0)
            for number, handler in saved_signals.items():
                signal.signal(number, handler)
            signal.setitimer(signal.ITIMER_REAL, *old_timer)
    result = {'schema_version':1,'status':status,'stop_reason':reason,'final_answer':ledger.final_answer,'model_calls':ledger.model_calls,'tool_calls':ledger.tool_calls,'usage':{'input_tokens':ledger.input_tokens if ledger.known else None,'output_tokens':ledger.output_tokens if ledger.known else None,'total_tokens':ledger.input_tokens+ledger.output_tokens if ledger.known else None,'complete':ledger.known},'capabilities':{'allowed_tools':sorted(allowed),'automatic_memory_extraction':False,'subagents':False,'teams':False,'background':False,'deadline':'cooperative; bash process groups bounded'}}
    result['security'] = policy.metadata(backend=ShellExecutor(policy).backend)
    result = ledger.redact_values(result)
    ledger.event('execution_stop', status=status, stop_reason=reason)
    (output/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    return result


def main(argv):
    parser = argparse.ArgumentParser(prog='pamu run')
    for flag in ('task-file','output','model'):
        parser.add_argument('--'+flag,required=True)
    for flag, default in [('max-model-calls',30),('max-tool-calls',100),('max-tokens',100000)]:
        parser.add_argument('--'+flag,type=int,default=default)
    parser.add_argument('--protocol', required=True, choices=('anthropic_messages','openai_chat','openai_responses'))
    parser.add_argument('--timeout',type=float,default=300)
    parser.add_argument('--replay')
    parser.add_argument('--sandbox', choices=('off', 'confined'))
    parser.add_argument('--allow-tool',action='append',dest='allowed_tools')
    args = vars(parser.parse_args(argv))
    try:
        task = Path(args.pop('task_file')).read_text()
        result = execute(task=task,workdir=Path.cwd(),**args)
    except (OSError, ValueError, TypeError):
        print('运行配置错误：请检查任务文件、输出目录和预算参数。', file=sys.stderr)
        return 2
    print(json.dumps(result,ensure_ascii=False))
    return 0 if result['status']=='completed' else 1
