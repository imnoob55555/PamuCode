"""Convert supported Pamu events to ATIF; reject incomplete call/result pairs."""


def to_atif(events, result, *, model, instruction=None):
    steps, pending, seen = [], {}, set()
    if instruction is not None:
        steps.append({'source': 'user', 'message': instruction})
    for event in events:
        kind = event.get('type')
        if kind == 'tool.started':
            identifier = event['tool_call_id']
            if identifier in seen:
                raise ValueError('duplicate tool call id')
            seen.add(identifier)
            step = {'source': 'agent', 'tool_calls': [{'tool_call_id': identifier,
                    'function_name': event['name'], 'arguments': event.get('input', {})}]}
            pending[identifier] = step
            steps.append(step)
        elif kind == 'tool.completed':
            step = pending.pop(event['tool_call_id'], None)
            if step is None:
                raise ValueError('observation has no tool call')
            step['observation'] = {'results': [{'source_call_id': event['tool_call_id'],
                                               'content': str(event.get('output', ''))}]}
    if pending:
        raise ValueError('incomplete tool observations; native events retained')
    if result.get('final_answer'):
        steps.append({'source': 'agent', 'message': result['final_answer']})
    if not steps:
        raise ValueError('no supported trajectory steps')
    for number, step in enumerate(steps, 1):
        step['step_id'] = number
    return {'schema_version': 'ATIF-v1.8', 'agent': {'name': 'pamucode', 'version': '0.1.0', 'model_name': model},
            'steps': steps, 'extra': {'execution_status': result.get('status'),
                                    'native_usage': result.get('usage'),
                                    'scope': 'tool observations and final answer; native events remain authoritative'}}
