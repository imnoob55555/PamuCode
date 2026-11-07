"""OpenAI Chat Completions conversion and indexed streaming tool assembly."""
from dataclasses import dataclass
from typing import Any

from .common import TextBlock, chat_history, check_thinking, finish, function_tools, null_progress, tool_block, value, visible_stream


@dataclass(frozen=True, slots=True)
class OpenAIChatAdapter:
    client: Any
    progress: Any = null_progress
    origin: str = ''

    def _request(self, system, messages, model, max_tokens, tools, thinking):
        check_thinking(thinking)
        request = {'model': model, 'messages': chat_history(system, messages), 'max_completion_tokens': max_tokens}
        if tools is not None:
            request['tools'] = function_tools(tools)
        return request

    def _response(self, message, reason):
        if reason not in ('stop', 'tool_calls', 'length') or value(message, 'refusal'):
            raise ValueError('Chat 请求失败或被拒绝')
        blocks = []
        text = value(message, 'content')
        if text:
            if not isinstance(text, str):
                raise ValueError('Chat 文本响应格式无效')
            blocks.append(TextBlock(text))
        for call in value(message, 'tool_calls', []) or []:
            if reason == 'length':
                continue
            function = value(call, 'function')
            blocks.append(tool_block(value(call, 'id'), value(function, 'name'), value(function, 'arguments')))
        has_calls = any(b.type == 'tool_use' for b in blocks)
        if (reason == 'tool_calls' and not has_calls) or (has_calls and reason == 'stop'):
            raise ValueError('Chat 工具调用与结束原因不一致')
        return finish(blocks, 'max_tokens' if reason == 'length' else 'tool_use' if has_calls else 'end_turn')

    def create(self, *, system, messages, model, max_tokens, tools, thinking=None):
        request = self._request(system, messages, model, max_tokens, tools, thinking)
        response = self.client.chat.completions.create(**request)
        choices = value(response, 'choices', [])
        if len(choices) != 1:
            raise ValueError('Chat 响应缺少唯一候选答案')
        return self._response(value(choices[0], 'message'), value(choices[0], 'finish_reason'))

    def create_streaming(self, *, system, messages, model, max_tokens, tools, thinking=None):
        request = self._request(system, messages, model, max_tokens, tools, thinking)
        with visible_stream(self.progress) as emit:
            stream = self.client.chat.completions.create(**request, stream=True)
            try:
                text, calls, reason = [], {}, None
                for event in stream:
                    if value(event, 'error'):
                        raise ValueError('Chat 流式请求失败')
                    for choice in value(event, 'choices', []):
                        if value(choice, 'index', 0) != 0 or reason is not None:
                            raise ValueError('Chat 流候选或结束顺序无效')
                        delta = value(choice, 'delta')
                        if value(delta, 'refusal'):
                            raise ValueError('模型拒绝回答请求')
                        chunk = value(delta, 'content')
                        if chunk:
                            emit(chunk)
                            text.append(chunk)
                        for part in value(delta, 'tool_calls', []) or []:
                            index = value(part, 'index')
                            if not isinstance(index, int) or index < 0:
                                raise ValueError('Chat 工具分片缺少有效索引')
                            call = calls.setdefault(index, {'id': '', 'function': {'name': '', 'arguments': ''}})
                            call['id'] += value(part, 'id') or ''
                            fn = value(part, 'function')
                            call['function']['name'] += value(fn, 'name') or ''
                            call['function']['arguments'] += value(fn, 'arguments') or ''
                        reason = value(choice, 'finish_reason')
                if reason is None:
                    raise ValueError('Chat 流缺少结束事件')
                if sorted(calls) != list(range(len(calls))):
                    raise ValueError('Chat 工具调用索引不连续')
                return self._response({'content': ''.join(text), 'tool_calls': [calls[i] for i in sorted(calls)]}, reason)
            finally:
                stream.close()
