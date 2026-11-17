"""Checkout-only evaluation CLI. Run with python -m pamu_eval."""
import argparse
import json
import sys
from pathlib import Path

from .runner import ROOT, run_suite, write_json
from .reporting import rebuild, compare_campaigns


def main(argv=None):
    parser = argparse.ArgumentParser(description='PamuCode 评测工具（源码仓库内使用）')
    sub = parser.add_subparsers(dest='command', required=True)
    run = sub.add_parser('run')
    run.add_argument('suite')
    run.add_argument('--output', required=True)
    run.add_argument('--resume', action='store_true')
    run.add_argument('--repetitions', type=int, default=1)
    run.add_argument('--jobs', type=int, default=1)
    run.add_argument('--model')
    run.add_argument('--protocol')
    run.add_argument('--backend', choices=('local', 'docker'), default='local',
                     help='local is trusted-suite execution; docker supports offline replay only')
    run.add_argument('--image', help='locally available Docker image pinned by sha256 digest')
    selftest = sub.add_parser('selftest')
    selftest.add_argument('--output', required=True)
    selftest.add_argument('--backend', choices=('local', 'docker'), default='local')
    selftest.add_argument('--image')
    report = sub.add_parser('report')
    report.add_argument('directory')
    comparison = sub.add_parser('compare')
    comparison.add_argument('base')
    comparison.add_argument('candidate')
    perf = sub.add_parser('perf')
    perf.add_argument('--output', required=True)
    export = sub.add_parser('swe-export')
    export.add_argument('input', help='JSON array of official prediction rows')
    export.add_argument('--output', required=True)
    patch = sub.add_parser('swe-patch')
    patch.add_argument('workspace')
    patch.add_argument('--base-commit', required=True)
    patch.add_argument('--output', required=True)
    replay = sub.add_parser('replay-export')
    replay.add_argument('events')
    replay.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command in {'run', 'selftest'}:
            suite = args.suite if args.command == 'run' else ROOT / 'evals' / 'regression.json'
            overrides = {key: getattr(args, key) for key in ('model', 'protocol') if getattr(args, key, None)}
            result = run_suite(suite, args.output, repetitions=getattr(args, 'repetitions', 1),
                               resume=getattr(args, 'resume', False), config_overrides=overrides,
                               jobs=getattr(args, 'jobs', 1),
                               backend=args.backend, image=args.image)
            print(json.dumps({key: value for key, value in result.items() if key != 'rows'}, ensure_ascii=False))
            return 0 if result['passed'] == result['total'] else 1
        if args.command == 'report':
            result = rebuild(args.directory)
            write_json(Path(args.directory) / 'report.json', result)
        elif args.command == 'compare':
            result = compare_campaigns(args.base, args.candidate)
        elif args.command == 'perf':
            from .performance import measure
            result = measure(args.output)
        elif args.command == 'swe-export':
            from .adapters.swebench import export_predictions
            result = {'predictions': export_predictions(json.loads(Path(args.input).read_text()), args.output)}
        elif args.command == 'replay-export':
            from .replay import export_replay
            result = {'responses': export_replay(args.events, args.output)}
        else:
            from .adapters.swebench import capture_patch
            Path(args.output).write_text(capture_patch(args.workspace, args.base_commit))
            result = {'patch': args.output}
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ValueError, OSError, KeyError) as error:
        print(f'评测错误：{error}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
