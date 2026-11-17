"""Fresh-process context-estimation measurements; descriptive, not a calibrated gate."""
import json
import os
import platform
import statistics
import subprocess
import sys
from pathlib import Path

from .runner import ROOT, code_identity, write_json


def measure(output, *, sizes=(100, 1000), repetitions=5):
    if repetitions < 1 or any(size < 1 for size in sizes):
        raise ValueError('positive workload sizes and repetitions required')
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    samples = []
    for size in sizes:
        for _ in range(repetitions):
            run = subprocess.run([sys.executable, '-m', 'pamu_eval.perf_worker', str(size)],
                                 cwd=ROOT, env={**os.environ, 'PYTHONPATH': str(ROOT)},
                                 capture_output=True, text=True, check=True, timeout=30)
            samples.append(json.loads(run.stdout))
    result = {'schema_version': 1, 'workload': 'production estimate_request, 256-character alternating messages',
              'scope': 'CPU context estimation only; excludes provider serialization, network, UI and persistence',
              'runtime': code_identity(), 'platform': platform.platform(), 'machine': platform.machine(),
              'cpu_count': os.cpu_count(), 'samples': samples,
              'median_wall_seconds': {str(size): statistics.median(r['wall_seconds'] for r in samples if r['messages'] == size) for size in sizes}}
    write_json(output / 'performance.json', result)
    return result
