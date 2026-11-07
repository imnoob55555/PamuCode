"""Native Ollama /api/chat adapter. The injected client owns its lifetime."""
import json
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from .common import TextBlock, attach_replay, chat_history, check_thinking, finish, function_tools, null_progress, tool_block, value, visible_stream


@dataclass(frozen=True, slots=True)
class OllamaAdapter:
    client: Any
    progress: Any = null_progress
    origin: str = ''

    def _request(self, system, messages, model, max_tokens, tools, thinking):
        check_thinking(thinking)
        request = {'model': model, 'messages': chat_history(system, messages, ollama=True, origin=self.origin, model=model), 'options': {'num_predict': max_tokens}}
        if tools is not None:
            request['tools'] = function_tools(tools)
        return request

    def _response(self, response, model):
        if value(response, 'error') or value(response, 'done') is not True:
            raise ValueError('Ollama 请求失败或缺少结束事件，请检查服务和已安装模型')
        reason = value(response, 'done_reason', 'stop')
        if reason not in ('stop', 'length'):
            raise ValueError('Ollama 返回未知结束原因')
        message = value(response, 'message')
        blocks = []
        text = value(message, 'content')
        if text:
            if not isinstance(text, str):
                raise ValueError('Ollama 文本响应格式无效')
            blocks.append(TextBlock(text))
        for call in value(message, 'tool_calls', []) or []:
            fn = value(call, 'function')
            blocks.append(tool_block('ollama_' + uuid4().hex, value(fn, 'name'), value(fn, 'arguments')))
        result = finish(blocks, 'max_tokens' if reason == 'length' else 'tool_use' if any(b.type == 'tool_use' for b in blocks) else 'end_turn')
        attach_replay(result.content, 'ollama', self.origin, model, message)
        return result

    def create(self, *, system, messages, model, max_tokens, tools, thinking=None):
        request = self._request(system, messages, model, max_tokens, tools, thinking)
        response = self.client.post('/api/chat', json={**request, 'stream': False})
        try:
            response.raise_for_status()
            return self._response(response.json(), model)
        finally:
            response.close()

    def create_streaming(self, *, system, messages, model, max_tokens, tools, thinking=None):
        request = self._request(system, messages, model, max_tokens, tools, thinking)
        with visible_stream(self.progress) as emit:
            with self.client.stream('POST', '/api/chat', json={**request, 'stream': True}) as response:
                response.raise_for_status()
                message = {'role': 'assistant', 'content': '', 'thinking': '', 'tool_calls': []}
                terminal = None
                for line in response.iter_lines():
                    if not line:
                        continue
                    if terminal is not None:
                        raise ValueError('Ollama 结束事件后仍有内容')
                    event = json.loads(line)
                    if value(event, 'error'):
                        raise ValueError('Ollama 流式请求失败，请检查服务和已安装模型')
                    delta = value(event, 'message', {})
                    text = value(delta, 'content', '')
                    emit(text)
                    message['content'] += text
                    message['thinking'] += value(delta, 'thinking', '')
                    message['tool_calls'].extend(value(delta, 'tool_calls', []) or [])
                    if value(event, 'done') is True:
                        terminal = event
                if terminal is None:
                    raise ValueError('Ollama 流缺少结束事件')
                return self._response({**terminal, 'message': message}, model)
