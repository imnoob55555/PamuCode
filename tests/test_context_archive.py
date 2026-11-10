import json
import uuid
import pytest
from agent_app.features.context_archive import ContextArchive
from agent_app.features.session_store import SessionError
from types import SimpleNamespace
from agent_app.runtime import ModelRouter, SessionState
from agent_app.features.sessions import SessionPersistence


def runtime(tmp_path):
    return SimpleNamespace(session=SessionState(), model=ModelRouter(primary_model='test'), sessions=SessionPersistence(tmp_path))



def test_dedup_identity_and_visible_only(tmp_path):
    archive = ContextArchive(tmp_path, str(uuid.uuid4()))
    history = [{'role': 'user', 'content': 'needle', '_pamu_id': 'a'}, {'role': 'user', 'content': 'needle', '_pamu_id': 'b'}]
    refs = archive.save_history(history)
    assert refs[0] == refs[1]
    assert len(list((archive.directory / 'objects').glob('*.json'))) == 1
    assert 'needle' in archive.search('needle')
    assert 'needle' in archive.read(refs[0])
    hidden = archive.save_history([{'role': 'assistant', 'content': [{'type': 'thinking', 'thinking': 'secret', 'signature': 'secret-signature'}, {'type': 'text', 'text': 'visible'}]}])[0]
    assert 'secret' not in archive.read(hidden)
    assert 'secret' not in archive.search('secret')


def test_controlled_paths_and_bounded_read(tmp_path):
    archive = ContextArchive(tmp_path, str(uuid.uuid4()))
    ref = archive.save_artifact('x' * 20000)
    result = archive.read(ref, limit=100)
    assert len(result) < 500
    assert 'next_offset' in result
    for ref in ('../../secret', 'artifact:' + '../' * 20):
        with pytest.raises(SessionError): archive.read(ref)


def test_symlink_object_rejected(tmp_path):
    archive = ContextArchive(tmp_path, str(uuid.uuid4()))
    ref = archive.save_artifact('safe')
    path = archive.directory / 'artifacts' / (ref.split(':')[1] + '.txt')
    path.unlink(); path.symlink_to(tmp_path / 'elsewhere')
    with pytest.raises(SessionError): archive.read(ref)


def test_candidate_failure_keeps_live_history(tmp_path, monkeypatch):
    from agent_app.features.sessions import checkpoint, commit_history
    rt = runtime(tmp_path)
    rt.session.history[:] = [{'role': 'user', 'content': 'original'}]
    rt.session.context_state = {'generation': 0}
    checkpoint(rt)
    saved = rt.sessions.store.load(rt.sessions.session_id)
    monkeypatch.setattr(rt.sessions.store, 'save', lambda snapshot: (_ for _ in ()).throw(OSError('fail')))
    with pytest.raises(SessionError):
        commit_history(rt, [{'role': 'user', 'content': 'summary'}], {'generation': 1})
    assert rt.session.history == saved['history']
    assert rt.session.context_state == saved['context_state']
    assert rt.sessions.store.load(rt.sessions.session_id) == saved
    rt.sessions.close()


def test_v1_migrates_only_on_save_and_keeps_backup(tmp_path):
    from tests.test_session_store import snapshot
    from agent_app.features.sessions import checkpoint
    rt = runtime(tmp_path)
    old = snapshot(tmp_path)
    old['archive'] = [[{'role': 'user', 'content': 'old evidence'}]]
    rt.sessions.store.save(old)
    rt.sessions.restore(rt, old, rt.sessions.store.acquire(old['id']))
    assert rt.sessions.store.load(old['id'])['schema_version'] == 1
    checkpoint(rt)
    new = rt.sessions.store.load(old['id'])
    assert new['schema_version'] == 2
    assert json.loads((rt.sessions.context_archive.directory / 'migration-v1.json').read_text()) == old
    assert 'old evidence' in rt.sessions.context_archive.read(new['archive'][0]['ref'])
    rt.sessions.close()


def test_identity_separates_equal_occurrences_and_survives_compaction(tmp_path):
    from agent_app.features.sessions import checkpoint, commit_history
    rt = runtime(tmp_path)
    rt.session.history[:] = [{'role': 'user', 'content': 'same'}, {'role': 'user', 'content': 'same'}]
    checkpoint(rt)
    identities = [r['id'] for r in rt.session.context_state['_message_records']]
    assert identities[0] != identities[1]
    candidate = [dict(rt.session.history[1])]
    # Explicit metadata resolves which equal occurrence is retained.
    candidate[0]['_pamu_id'] = identities[1]
    commit_history(rt, candidate, {'generation': 1})
    saved = rt.sessions.store.load(rt.sessions.session_id)
    assert {r['id'] for r in saved['archive']} == set(identities)
    assert len({r['ref'] for r in saved['archive']}) == 1
    assert saved['context_state']['_message_records'][0]['id'] == identities[1]
    rt.sessions.close()


def test_search_continues_into_large_artifact(tmp_path):
    archive = ContextArchive(tmp_path, str(uuid.uuid4()))
    archive.save_artifact('x' * (8 * 1024 * 1024 + 100) + 'tail needle')
    first = json.loads(archive.search('tail needle'))
    assert not first['complete']
    assert first['cursor']
    assert first['scanned_bytes'] <= 8 * 1024 * 1024
    second = json.loads(archive.search('tail needle', cursor=first['cursor']))
    assert second['complete']
    assert second['matches']


def test_large_tool_evidence_is_deduplicated_artifact(tmp_path):
    archive = ContextArchive(tmp_path, str(uuid.uuid4()))
    original = 'result' * 5000
    ref = archive.save_history([{'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'call', 'content': original}]}])[0]
    visible = archive._read(ref)
    assert 'artifact:' in visible
    assert len(visible) < 2000
    assert len(list((archive.directory / 'artifacts').glob('*.txt'))) == 1
    assert 'result' in archive.search('result')


def test_artifact_corruption_rejected_for_read_and_search(tmp_path):
    archive = ContextArchive(tmp_path, str(uuid.uuid4()))
    ref = archive.save_artifact('trusted evidence')
    (archive.directory / 'artifacts' / (ref.split(':')[1] + '.txt')).write_text('corrupt evidence')
    with pytest.raises(SessionError):
        archive.read(ref)
    result = json.loads(archive.search('corrupt'))
    assert not result['matches']
    assert not result['complete']
    assert result['warnings']


def test_uncommitted_archive_objects_are_not_searchable(tmp_path):
    from agent_app.features.sessions import checkpoint
    rt = runtime(tmp_path)
    rt.session.history[:] = [{'role': 'user', 'content': 'current'}]
    checkpoint(rt)
    archive = rt.sessions.context_archive
    ref = archive.save_history([{'role': 'user', 'content': 'uncommitted secret'}])[0]
    assert not json.loads(archive.search('uncommitted'))['matches']
    with pytest.raises(SessionError):
        archive.read(ref)
    rt.sessions.close()


def test_missing_active_artifact_blocks_restore(tmp_path):
    from agent_app.features.sessions import checkpoint
    rt = runtime(tmp_path)
    rt.session.history[:] = [{'role': 'user', 'content': 'current'}]
    checkpoint(rt)
    ref = rt.sessions.context_archive.save_artifact('tool evidence')
    rt.session.history.append({'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'call', 'content': '[归档结果 ' + ref + ']'}]})
    checkpoint(rt)
    path = rt.sessions.context_archive.directory / 'artifacts' / (ref.split(':')[1] + '.txt')
    path.unlink()
    with pytest.raises(SessionError):
        rt.sessions.store.load(rt.sessions.session_id)
    rt.sessions.close()


def test_missing_historical_evidence_warns_without_blocking_active_snapshot(tmp_path):
    from agent_app.features.sessions import checkpoint, commit_history
    rt = runtime(tmp_path)
    rt.session.history[:] = [{'role': 'user', 'content': 'old evidence'}]
    checkpoint(rt)
    commit_history(rt, [{'role': 'user', 'content': 'summary'}], {'generation': 1})
    saved = rt.sessions.store.load(rt.sessions.session_id)
    ref = saved['archive'][0]['ref']
    (rt.sessions.context_archive.directory / 'objects' / (ref.split(':')[1] + '.json')).unlink()
    assert rt.sessions.store.load(rt.sessions.session_id)['history'] == rt.session.history
    result = json.loads(rt.sessions.context_archive.search('old evidence'))
    assert not result['complete']
    assert result['warnings']
    rt.sessions.close()
