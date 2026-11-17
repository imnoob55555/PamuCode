"""One isolated performance sample."""
import json
import os
import resource
import sys
import time

from agent_app.core.context_budget import estimate_request


def main():
    count = int(sys.argv[1])
    messages = [{'role': 'user' if i % 2 == 0 else 'assistant', 'content': 'a' * 256} for i in range(count)]
    cpu, wall = time.process_time(), time.perf_counter()
    tokens = estimate_request('system', messages, [])
    wall, cpu = time.perf_counter() - wall, time.process_time() - cpu
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    print(json.dumps({'pid': os.getpid(), 'messages': count, 'estimated_tokens': tokens,
                      'wall_seconds': wall, 'cpu_seconds': cpu,
                      'peak_rss_bytes': rss if sys.platform == 'darwin' else rss * 1024}))


if __name__ == '__main__':
    main()
