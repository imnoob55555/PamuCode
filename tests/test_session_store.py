import copy
import json
import multiprocessing
import os
from pathlib import Path
import uuid
import pytest
from agent_app.features.session_store import SessionStore, SessionError


def snapshot(workdir, id=None, updated=1):
    return dict(schema_version=1, id=id or str(uuid.uuid4()), workspace=str(workdir.resolve()), created_at=1., updated_at=updated, title='hello', history=[{'role': 'user', 'content': 'hello'}], archive=[], todos=[], rounds_since_todo=0, selection=dict(provider='anthropic', protocol='anthropic_messages', base_url='https://api.anthropic.com', primary_model='dummy', fallback_model=None, thinking_level='off'), status='idle', tool_states={})


def test_missing_read_no_side_effect(tmp_path):
    store = SessionStore(tmp_path)
    assert store.list_sessions() == ([], 0)
    with pytest.raises(SessionError): store.load('12345678')
    assert not (tmp_path / '.pamu').exists()


def test_roundtrip_sort_select_and_release(tmp_path):
    store = SessionStore(tmp_path)
    a, b = snapshot(tmp_path), snapshot(tmp_path, updated=2)
    for s in (a,b):
        with store.acquire(s['id']): store.save(s)
    assert store.load(a['id'][:8]) == a
    assert [s['id'] for s in store.list_sessions()[0]] == [b['id'], a['id']]
    lease = store.acquire(a['id'])
    with pytest.raises(SessionError): store.acquire(a['id'])
    lease.close(); lease.close()
    with store.acquire(a['id']): pass
    assert (tmp_path / '.pamu/.gitignore').read_text() == '*\n!.gitignore\n'
    assert os.stat(tmp_path / '.pamu/sessions' / (a['id']+'.json')).st_mode & 0o777 == 0o600


@pytest.mark.parametrize('change', [dict(schema_version=2), dict(workspace='/elsewhere'), dict(history=[{'role':'user','content':[{'type':'unknown'}]}]), dict(selection={'api_key': 'fake'}), dict(updated_at=float('nan')), dict(todos=[{'content':'x','status':'bad'}]), dict(tool_states={'x':'bad'})])
def test_invalid_snapshots(tmp_path, change):
    s = snapshot(tmp_path); s.update(change)
    with pytest.raises(SessionError): SessionStore(tmp_path).save(s)


def test_ambiguous_and_corrupt(tmp_path):
    store = SessionStore(tmp_path)
    for tail in ('000000000001','000000000002'):
        s = snapshot(tmp_path, '12345678-0000-4000-8000-'+tail)
        store.save(s)
    with pytest.raises(SessionError): store.load('12345678')
    with pytest.raises(SessionError): store.load('123')
    (tmp_path / '.pamu/sessions' / (str(uuid.uuid4())+'.json')).write_text('{}')
    assert store.list_sessions()[1] == 1


@pytest.mark.parametrize('component', ['.pamu', '.pamu/sessions'])
def test_symlink_directories(tmp_path, component):
    target = tmp_path / 'target'; target.mkdir()
    link = tmp_path / component; link.parent.mkdir(exist_ok=True); link.symlink_to(target, target_is_directory=True)
    with pytest.raises(SessionError): SessionStore(tmp_path).save(snapshot(tmp_path))


def test_unsafe_files_and_lock(tmp_path):
    store = SessionStore(tmp_path); s = snapshot(tmp_path); store.save(s)
    path = tmp_path / '.pamu/sessions' / (s['id']+'.json')
    path.unlink(); path.symlink_to(tmp_path / 'missing')
    with pytest.raises(SessionError): store.load(s['id'])
    with pytest.raises(SessionError): store.save(s)
    lock = path.with_suffix('.lock'); lock.symlink_to(tmp_path / 'missing')
    with pytest.raises(SessionError): store.acquire(s['id'])
    path.unlink(); path.mkdir()
    with pytest.raises(SessionError): store.load(s['id'])


def test_atomic_failure(tmp_path, monkeypatch):
    store = SessionStore(tmp_path); s = snapshot(tmp_path); store.save(s)
    monkeypatch.setattr(os, 'replace', lambda *args: (_ for _ in ()).throw(OSError('fail')))
    changed = copy.deepcopy(s); changed['title'] = 'changed'
    with pytest.raises(SessionError): store.save(changed)
    assert store.load(s['id']) == s
    assert not list((tmp_path / '.pamu/sessions').glob('*.tmp'))


def contend(workdir, id, queue):
    try:
        with SessionStore(Path(workdir)).acquire(id): queue.put('acquired')
    except SessionError: queue.put('locked')


def test_process_lock(tmp_path):
    store = SessionStore(tmp_path); id = str(uuid.uuid4())
    ctx = multiprocessing.get_context('spawn'); queue = ctx.Queue()
    with store.acquire(id):
        process = ctx.Process(target=contend, args=(str(tmp_path), id, queue)); process.start(); process.join(10)
        assert queue.get(timeout=2) == 'locked'
    process = ctx.Process(target=contend, args=(str(tmp_path), id, queue)); process.start(); process.join(10)
    assert queue.get(timeout=2) == 'acquired'


def test_private_directories_and_invalid_archive(tmp_path):
    store = SessionStore(tmp_path); s = snapshot(tmp_path)
    store.save(s)
    assert (tmp_path / '.pamu').stat().st_mode & 0o777 == 0o700
    assert (tmp_path / '.pamu/sessions').stat().st_mode & 0o777 == 0o700
    s['archive'] = [[{'role': 'assistant', 'content': [{'type': 'unknown'}]}]]
    with pytest.raises(SessionError): store.save(s)


def test_oversized_timestamp_is_safe_error(tmp_path):
    s = snapshot(tmp_path); s['created_at'] = 10 ** 400
    with pytest.raises(SessionError): SessionStore(tmp_path).save(s)
