"""Extract complete sequential model output streams as reviewed replay fixtures."""
import json
from pathlib import Path


def export_replay(events, output):
    rows = [json.loads(line) for line in Path(events).read_text().splitlines() if line.strip()]
    responses = []
    requests = sum(row.get('type') == 'model_request' for row in rows)
    for row in rows:
        if row.get('type') == 'model.completed':
            response = dict(row['response'])
            response['usage'] = row.get('usage')
            responses.append(response)
    if not responses or len(responses) != requests:
        raise ValueError('incomplete model calls cannot be exported as a successful replay script')
    Path(output).write_text(json.dumps(responses, indent=2, ensure_ascii=False) + '\n')
    return len(responses)
