import json
import os
import sys
import time
from pathlib import Path

import pytest


def test_external_grader_positive_and_negative(tmp_path):
    from pamu_eval.grading import grade
    checks = [{'type': 'file_contains', 'path': 'answer.txt', 'value': 'correct'}]
    assert grade(tmp_path, checks)['passed'] is False
    (tmp_path / 'answer.txt').write_text('correct')
    assert grade(tmp_path, checks)['passed'] is True
    (tmp_path / 'answer.txt').write_text('incorrect')
    assert grade(tmp_path, [{'type': 'file_equals', 'path': 'answer.txt', 'value': 'correct'}])['passed'] is False


def test_grader_rejects_escape(tmp_path):
    from pamu_eval.grading import grade
    with pytest.raises(ValueError):
        grade(tmp_path, [{'type': 'file_equals', 'path': '../secret', 'value': ''}])


def test_supervisor_timeout_kills_descendants(tmp_path):
    from pamu_eval.process import execute
    marker = tmp_path / 'orphan'
    code = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',sys.argv[1]]); time.sleep(10)"
    child = f"import time; from pathlib import Path; time.sleep(.7); Path({str(marker)!r}).write_text('bad')"
    result = execute([sys.executable, '-c', code, child], tmp_path, tmp_path / 'logs', .15)
    assert result['timed_out']
    time.sleep(.85)
    assert not marker.exists()


def test_suite_fingerprint_tracks_fixture_and_rejects_duplicates(tmp_path):
    from pamu_eval.suite import load_suite
    (tmp_path / 'fixture').mkdir()
    suite = tmp_path / 'suite.json'
    task = {'id': 'edit', 'prompt': 'fix', 'fixture': 'fixture', 'checks': [{'type': 'file_exists', 'path': 'answer'}]}
    suite.write_text(json.dumps({'schema_version': 1, 'tasks': [task]}))
    first = load_suite(suite)['fingerprint']
    (tmp_path / 'fixture' / 'new').write_text('changed')
    assert load_suite(suite)['fingerprint'] != first
    suite.write_text(json.dumps({'schema_version': 1, 'tasks': [task, task]}))
    with pytest.raises(ValueError, match='duplicate'):
        load_suite(suite)


def test_report_preserves_denominator_and_unknown_usage():
    from pamu_eval.reporting import summarize, compare
    rows = [
        {'task_id': 'a', 'repetition': 1, 'outcome': 'passed', 'usage': {'input_tokens': 5, 'output_tokens': 2}, 'duration_seconds': 1},
        {'task_id': 'b', 'repetition': 1, 'outcome': 'infra_error', 'usage': None, 'duration_seconds': 2},
    ]
    report = summarize(rows)
    assert report['pass_rate'] == .5
    assert report['usage']['complete'] is False
    assert report['usage']['input_tokens'] is None
    candidate = [dict(row, outcome='failed') for row in rows]
    changes = compare(rows, candidate)
    assert changes['regressions'] == ['a#1']


def test_comparison_refuses_changed_task_definitions(tmp_path):
    from pamu_eval.reporting import compare_campaigns
    for name, prompt in [('base', 'first'), ('candidate', 'different')]:
        directory = tmp_path / name
        directory.mkdir()
        (directory / 'manifest.json').write_text(json.dumps({'tasks': [{'id': 'a', 'prompt': prompt}], 'config': {}, 'repetitions': 1}))
    with pytest.raises(ValueError, match='task definitions'):
        compare_campaigns(tmp_path / 'base', tmp_path / 'candidate')


def test_command_grader_records_exit_code_and_timeout(tmp_path):
    from pamu_eval.grading import grade
    result = grade(tmp_path, [{'type': 'command', 'argv': [sys.executable, '-c', 'raise SystemExit(3)']}])
    assert result['checks'][0]['exit_code'] == 3
    assert not result['passed']


def test_suite_rejects_symlink_fixture(tmp_path):
    from pamu_eval.suite import load_suite
    fixture = tmp_path / 'fixture'
    fixture.mkdir()
    (fixture / 'escape').symlink_to(tmp_path / 'private')
    p = tmp_path / 'suite.json'
    p.write_text(json.dumps({'schema_version': 1, 'tasks': [{'id': 'a', 'prompt': 'a', 'fixture': 'fixture', 'checks': [{'type': 'file_exists', 'path': 'x'}]}]}))
    with pytest.raises(ValueError, match='symlink'):
        load_suite(p)


def test_fingerprints_track_permissions_and_empty_directories(tmp_path):
    from pamu_eval.suite import fixture_digest
    from pamu_eval.evidence import workspace_hash
    root = tmp_path / 'fixture'
    root.mkdir()
    script = root / 'script'
    script.write_text('echo ok')
    script.chmod(0o644)
    before = fixture_digest(root), workspace_hash(root)
    script.chmod(0o755)
    assert (fixture_digest(root), workspace_hash(root)) != before
    chmod = fixture_digest(root)
    (root / 'empty').mkdir()
    assert fixture_digest(root) != chmod


def test_runner_isolates_repetitions_and_checks_resume_evidence(tmp_path):
    from pamu_eval.runner import run_suite
    fixture = tmp_path / 'fixture'
    fixture.mkdir()
    (fixture / 'untouched').write_text('original')
    agent = tmp_path / 'fake_agent.py'
    agent.write_text('''import argparse,json
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--output');a,_=p.parse_known_args()
assert not Path('answer').exists()
Path('answer').write_text('correct')
out=Path(a.output);out.mkdir(parents=True,exist_ok=True)
(out/'events.jsonl').write_text('')
(out/'result.json').write_text(json.dumps({'status':'completed','usage':{'input_tokens':1,'output_tokens':1}}))
''')
    suite = tmp_path / 'suite.json'
    suite.write_text(json.dumps({'schema_version': 1, 'tasks': [{'id': 'a', 'prompt': 'fix', 'fixture': 'fixture', 'checks': [{'type': 'file_equals', 'path': 'answer', 'value': 'correct'}]}]}))
    output = tmp_path / 'output'
    command = [sys.executable, str(agent)]
    report = run_suite(suite, output, repetitions=2, agent_command=command, jobs=2)
    assert report['pass_rate'] == 1
    assert not (fixture / 'answer').exists()
    assert run_suite(suite, output, repetitions=2, resume=True, agent_command=command)['total'] == 2
    row_path = output / 'trials' / 'a--1' / 'trial.json'
    original = row_path.read_text()
    row_path.write_text(original.replace('"passed"', '"failed"'))
    with pytest.raises(ValueError, match='evidence'):
        run_suite(suite, output, repetitions=2, resume=True, agent_command=command)
    row_path.write_text(original)
    answer = output / 'trials' / 'a--1' / 'workspace' / 'answer'
    answer.write_text('tampered')
    with pytest.raises(ValueError, match='evidence'):
        run_suite(suite, output, repetitions=2, resume=True, agent_command=command)
    answer.write_text('correct')
    (output / 'trials' / 'a--1' / 'agent' / 'result.json').write_text('{}')
    with pytest.raises(ValueError, match='evidence'):
        run_suite(suite, output, repetitions=2, resume=True, agent_command=command)


def test_report_rejects_changed_manifest_denominator(tmp_path):
    from pamu_eval.runner import run_suite
    from pamu_eval.reporting import rebuild
    fixture = tmp_path / 'fixture'
    fixture.mkdir()
    suite = tmp_path / 'suite.json'
    suite.write_text(json.dumps({'schema_version': 1, 'tasks': [{'id': 'a', 'prompt': 'fix', 'fixture': 'fixture', 'checks': [{'type': 'file_exists', 'path': 'answer'}]}]}))
    output = tmp_path / 'output'
    run_suite(suite, output, agent_command=[sys.executable, '-c', 'pass'])
    path = output / 'manifest.json'
    manifest = json.loads(path.read_text())
    manifest['tasks'] = []
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='manifest'):
        rebuild(output)


def test_runner_resume_rejects_changed_fixture(tmp_path):
    from pamu_eval.runner import run_suite
    fixture = tmp_path / 'fixture'
    fixture.mkdir()
    suite = tmp_path / 'suite.json'
    suite.write_text(json.dumps({'schema_version': 1, 'tasks': [{'id': 'a', 'prompt': 'fix', 'fixture': 'fixture', 'checks': [{'type': 'file_exists', 'path': 'answer'}]}]}))
    output = tmp_path / 'output'
    run_suite(suite, output, agent_command=[sys.executable, '-c', 'pass'])
    (fixture / 'new').write_text('changed')
    with pytest.raises(ValueError, match='identity'):
        run_suite(suite, output, resume=True, agent_command=[sys.executable, '-c', 'pass'])


def test_environment_identity_tracks_routing_without_secrets(monkeypatch):
    from pamu_eval.runner import environment_identity
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'private-key')
    monkeypatch.setenv('ANTHROPIC_BASE_URL', 'https://first.invalid')
    first = environment_identity()
    assert 'private-key' not in json.dumps(first)
    monkeypatch.setenv('ANTHROPIC_BASE_URL', 'https://second.invalid')
    assert environment_identity() != first


def test_performance_command_emits_raw_fresh_process_samples(tmp_path):
    from pamu_eval.performance import measure
    result = measure(tmp_path, sizes=(10, 20), repetitions=2)
    assert len(result['samples']) == 4
    assert len({r['pid'] for r in result['samples']}) == 4
    assert all(r['estimated_tokens'] > 0 for r in result['samples'])
    assert all(r['wall_seconds'] >= 0 for r in result['samples'])


def test_shipped_regression_runs_actual_headless_and_external_checks(tmp_path):
    from pamu_eval.runner import ROOT, run_suite
    result = run_suite(ROOT / 'evals' / 'regression.json', tmp_path / 'campaign', repetitions=2, jobs=2)
    assert result['total'] == 4 and result['passed'] == 4
    for task in ('write-and-verify', 'recover-tool-error'):
        for repetition in (1, 2):
            directory = tmp_path / 'campaign' / 'trials' / f'{task}--{repetition}'
            assert json.loads((directory / 'grades.json').read_text())['passed']
            assert json.loads((directory / 'agent' / 'result.json').read_text())['tool_calls'] > 0


def test_export_replay_roundtrip_and_refuses_interrupted_calls(tmp_path):
    from pamu_eval.replay import export_replay
    response = {'content': [{'type': 'text', 'text': 'done'}], 'stop_reason': 'end_turn'}
    events = tmp_path / 'events.jsonl'
    rows = [{'type': 'model_request'}, {'type': 'model.completed', 'response': response, 'usage': {'input_tokens': 1, 'output_tokens': 2}}]
    events.write_text('\n'.join(json.dumps(row) for row in rows))
    output = tmp_path / 'replay.json'
    assert export_replay(events, output) == 1
    assert json.loads(output.read_text())[0]['usage']['input_tokens'] == 1
    events.write_text(events.read_text() + '\n' + json.dumps({'type': 'model_request'}))
    with pytest.raises(ValueError, match='incomplete'):
        export_replay(events, output)
