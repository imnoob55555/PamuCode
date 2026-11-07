"""OpenAI Responses API, including stateless encrypted reasoning replay."""
import json
from dataclasses import dataclass
from typing import Any

from .common import (
    TextBlock, arguments, attach_replay, check_thinking, finish, function_tools,
    generic_blocks, native_replay, null_progress, output_text, tool_block,
    value, visible_stream,
)


@dataclass(frozen=True, slots=True)
class OpenAIResponsesAdapter:
    client: Any
    progress: Any = null_progress
    origin: str = ''

    def _request(self, system, messages, model, max_tokens, tools, thinking):
        check_thinking(thinking)
        items, ids = [], set()
        for message in messages:
            content = value(message, 'content')
            role = value(message, 'role')
            blocks = generic_blocks(content)
            native = native_replay(content, 'openai_responses', self.origin, model) if role == 'assistant' else None
            if native is not None:
                items.extend(native)
                ids.update(b['id'] for b in blocks if b['type'] == 'tool_use')
                continue
            for block in blocks:
                kind = block['type']
                if kind == 'text' and block['text']:
                    items.append({'role': role, 'content': block['text']})
                elif kind == 'tool_use':
                    ids.add(block['id'])
                    items.append({'type': 'function_call', 'call_id': block['id'], 'name': block['name'], 'arguments': json.dumps(block['input'], separators=(',', ':'), ensure_ascii=False)})
                elif kind == 'tool_result':
                    if block['tool_use_id'] not in ids:
                        raise ValueError('工具结果缺少对应调用')
                    items.append({'type': 'function_call_output', 'call_id': block['tool_use_id'], 'output': output_text(block['content'])})
        request = {'model': model, 'input': items, 'max_output_tokens': max_tokens,
                   'store': False, 'include': ['reasoning.encrypted_content']}
        if system is not None:
            request['instructions'] = output_text(system)
        if tools is not None:
            request['tools'] = function_tools(tools, responses=True)
        return request

    def _response(self, response, model):
        if value(response, 'error'):
            raise ValueError('Responses 请求返回错误')
        status = value(response, 'status')
        reason = 'end_turn'
        if status == 'incomplete' and value(value(response, 'incomplete_details'), 'reason') == 'max_output_tokens':
            reason = 'max_tokens'
        elif status != 'completed':
            raise ValueError('Responses 请求未完成或失败')
        blocks = []
        output = value(response, 'output', [])
        for item in output:
            kind = value(item, 'type')
            if kind == 'function_call':
                if reason == 'max_tokens':
                    # Discard the unfinished generation's calls; core can retry
                    # with a larger budget without executing partial arguments.
                    continue
                if value(item, 'status') not in (None, 'completed') and reason != 'max_tokens':
                    raise ValueError('模型返回未完成的工具调用')
                blocks.append(tool_block(value(item, 'call_id'), value(item, 'name'), value(item, 'arguments')))
            elif kind == 'message':
                if value(item, 'status') not in (None, 'completed') and reason != 'max_tokens':
                    raise ValueError('模型返回未完成的消息')
                for part in value(item, 'content', []):
                    if value(part, 'type') == 'refusal':
                        raise ValueError('模型拒绝回答请求')
                    if value(part, 'type') == 'output_text' and value(part, 'text'):
                        text = value(part, 'text')
                        if not isinstance(text, str):
                            raise ValueError('Responses 文本响应格式无效')
                        blocks.append(TextBlock(text))
            elif kind != 'reasoning':
                raise ValueError('Responses 返回不支持的内容类型')
        if reason != 'max_tokens' and any(b.type == 'tool_use' for b in blocks):
            reason = 'tool_use'
        result = finish(blocks, reason)
        if reason != 'max_tokens':
            attach_replay(result.content, 'openai_responses', self.origin, model, output)
        return result

    def create(self, *, system, messages, model, max_tokens, tools, thinking=None):
        request = self._request(system, messages, model, max_tokens, tools, thinking)
        return self._response(self.client.responses.create(**request), model)

    @staticmethod
    def _call_event(event, calls):
        kind = value(event, 'type')
        item = value(event, 'item')
        if kind.startswith('response.output_item.') and value(item, 'type') != 'function_call':
            return
        index = value(event, 'output_index')
        if not isinstance(index, int) or index < 0:
            raise ValueError('Responses 工具分片缺少有效索引')
        call = calls.setdefault(index, {'arguments': '', 'fragmented': False})
        item_id = value(event, 'item_id') or value(item, 'id')
        if call.get('id') and item_id != call['id']:
            raise ValueError('Responses 工具分片 ID 不一致')
        call['id'] = item_id
        if kind == 'response.function_call_arguments.delta':
            delta = value(event, 'delta')
            if not isinstance(delta, str):
                raise ValueError('Responses 工具参数分片格式无效')
            call['arguments'] += delta
            call['fragmented'] = True
        elif kind in ('response.function_call_arguments.done', 'response.output_item.done'):
            raw = value(event, 'arguments') if item is None else value(item, 'arguments')
            # Validate only after the terminal event determines whether the
            # generation completed or ran out of its output budget.
            call['completed_arguments'] = raw
            if not call['fragmented']:
                call['arguments'] = raw
            if item is not None:
                call.update(name=value(item, 'name'), call_id=value(item, 'call_id'))
        elif kind == 'response.output_item.added':
            call.update(name=value(item, 'name'), call_id=value(item, 'call_id'))
            call['arguments'] = value(item, 'arguments', '')

    @staticmethod
    def _validate_stream_calls(calls, response):
        output = value(response, 'output', [])
        for index, call in calls.items():
            if index >= len(output):
                raise ValueError('Responses 工具分片缺少最终内容')
            final = output[index]
            if value(final, 'type') != 'function_call':
                raise ValueError('Responses 工具分片最终类型不一致')
            for key in ('id', 'name', 'call_id'):
                if call.get(key) and call[key] != value(final, key):
                    raise ValueError('Responses 工具分片与最终调用不一致')
            if arguments(call['arguments']) != arguments(value(final, 'arguments')):
                raise ValueError('Responses 工具分片与最终参数不一致')
            if 'completed_arguments' in call and arguments(call['completed_arguments']) != arguments(value(final, 'arguments')):
                raise ValueError('Responses 工具结束分片与最终参数不一致')

    def create_streaming(self, *, system, messages, model, max_tokens, tools, thinking=None):
        request = self._request(system, messages, model, max_tokens, tools, thinking)
        with visible_stream(self.progress) as emit:
            stream = self.client.responses.create(**request, stream=True)
            try:
                terminal, calls = None, {}
                for event in stream:
                    kind = value(event, 'type')
                    if kind in ('error', 'response.failed', 'response.error', 'response.cancelled'):
                        raise ValueError('Responses 流式请求失败')
                    if terminal is not None:
                        raise ValueError('Responses 结束事件后仍有内容')
                    if kind == 'response.output_text.delta':
                        emit(value(event, 'delta', ''))
                    elif kind in ('response.output_item.added', 'response.output_item.done', 'response.function_call_arguments.delta', 'response.function_call_arguments.done'):
                        self._call_event(event, calls)
                    elif kind in ('response.completed', 'response.incomplete'):
                        terminal = value(event, 'response')
                if terminal is None:
                    raise ValueError('Responses 流缺少结束事件')
                if value(terminal, 'status') == 'completed':
                    self._validate_stream_calls(calls, terminal)
                return self._response(terminal, model)
            finally:
                stream.close()
