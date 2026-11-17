"""Rebuildable reports without silently dropping unsuccessful trials."""
import json
import math
import statistics
from collections import Counter
from pathlib import Path
from .evidence import validate
from .suite import digest


def summarize(rows):
    total = len(rows)
    passed = sum(row['outcome'] == 'passed' for row in rows)
    proportion = passed / total if total else None
    interval = None
    if total:
        z = 1.96
        center = (proportion + z*z/(2*total))/(1+z*z/total)
        half = z*math.sqrt(proportion*(1-proportion)/total + z*z/(4*total*total))/(1+z*z/total)
        interval = [max(0, center-half), min(1, center+half)]
    complete = bool(rows) and all(isinstance(row.get('usage'), dict) and
        row['usage'].get('input_tokens') is not None and row['usage'].get('output_tokens') is not None
        and row['usage'].get('complete', True) for row in rows)
    usage = {'complete': complete}
    for field in ('input_tokens', 'output_tokens'):
        known = sum((row.get('usage') or {}).get(field) or 0 for row in rows)
        usage[field] = known if complete else None
        usage['known_' + field] = known
    per_task = {}
    for row in rows:
        per_task.setdefault(row['task_id'], []).append(row['outcome'] == 'passed')
    durations = [row['duration_seconds'] for row in rows if row.get('duration_seconds') is not None]
    return {'total': total, 'passed': passed, 'pass_rate': proportion,
            'outcomes': dict(Counter(row['outcome'] for row in rows)), 'usage': usage,
            'trial_pass_rate_wilson_95': interval,
            'interval_note': 'Descriptive trial-level Wilson interval; repeated trials of a task are not independent tasks.',
            'per_task': {key: {'trials': len(values), 'pass_rate': sum(values)/len(values),
                              'all_trials_passed': all(values)} for key, values in per_task.items()},
            'median_seconds': statistics.median(durations) if durations else None,
            'categories': {category: {'total': sum(row.get('category') == category for row in rows),
                                    'passed': sum(row.get('category') == category and row['outcome'] == 'passed' for row in rows)}
                           for category in sorted({row.get('category', 'uncategorized') for row in rows})},
            'rows': rows}


def rebuild(directory):
    directory = Path(directory)
    manifest = json.loads((directory / 'manifest.json').read_text())
    if manifest.get('identity') != digest({key: value for key, value in manifest.items() if key != 'identity'}):
        raise ValueError('campaign manifest identity changed')
    rows = []
    for task in manifest['tasks']:
        for repetition in range(1, manifest['repetitions'] + 1):
            path = directory / 'trials' / f'{task["id"]}--{repetition}' / 'trial.json'
            if path.exists():
                rows.append(validate(path.parent))
            else:
                rows.append({'task_id': task['id'], 'repetition': repetition,
                             'outcome': 'missing', 'usage': None, 'duration_seconds': None})
    return summarize(rows)


def compare(base, candidate):
    def indexed(rows):
        result = {}
        for row in rows:
            key = f'{row["task_id"]}#{row["repetition"]}'
            if key in result:
                raise ValueError('duplicate trial identity')
            result[key] = row['outcome'] == 'passed'
        return result
    a, b = indexed(base), indexed(candidate)
    common = a.keys() & b.keys()
    return {'paired_trials': len(common), 'regressions': sorted(k for k in common if a[k] and not b[k]),
            'improvements': sorted(k for k in common if b[k] and not a[k]),
            'unmatched_base': sorted(a.keys()-b.keys()), 'unmatched_candidate': sorted(b.keys()-a.keys())}


def compare_campaigns(base, candidate):
    manifests = [json.loads((Path(directory) / 'manifest.json').read_text()) for directory in (base, candidate)]
    def definitions(manifest):
        return {task['id']: {key: value for key, value in task.items() if key != 'fixture'} for task in manifest['tasks']}
    a, b = map(definitions, manifests)
    if any(a[key] != b[key] for key in a.keys() & b.keys()):
        raise ValueError('shared task definitions changed; cannot report paired improvements')
    result = compare(rebuild(base)['rows'], rebuild(candidate)['rows'])
    left, right = (manifest['config'] for manifest in manifests)
    result['configuration_changes'] = {key: {'base': left.get(key), 'candidate': right.get(key)}
                                       for key in left.keys() | right.keys() if left.get(key) != right.get(key)}
    result['routing_changed'] = manifests[0].get('routing_fingerprints') != manifests[1].get('routing_fingerprints')
    result['note'] = 'System comparison; causal harness claims require fixed model, environment and budgets.'
    return result
