"""网页: 会话分类、例行任务、整理方案、记忆(归档、字段、只读索引)、总览。"""
import time

import pytest
from starlette.testclient import TestClient

from larkx import groups, jobs, organize
from larkx.storage import Storage
from larkx.web import app as web

H = {'X-Larkx': '1'}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(web, 'TOKEN_PATH', tmp_path / 'web_token')
    monkeypatch.setattr(web, '_login_state', lambda: {'ok': True, 'message': '', 'user_id': '100'})
    monkeypatch.setenv('LARKX_AGENT_HOME', str(tmp_path / 'agent'))
    c = TestClient(web.create_app())
    c.post('/login', json={'token': web.get_token()})
    st = Storage()
    for cid, name in (('11', '项目群'), ('12', '大群')):
        st.save_message({'msg_id': f'm{cid}', 'chat_id': cid, 'chat_type': 2, 'from_id': '200', 'content': 'x',
                         'create_time': int(time.time())}, chat_name=name)
    groups.ChatStore(st).save_profile('12', '100', name='大群', chat_type=2, members=999, msgs=1)
    return c


def test_groups_and_chat_assignment(client):
    assert client.post('/api/groups', json={'name': 'X'}).status_code == 403          # 缺请求头
    gs = client.post('/api/groups', json={'name': 'InfraBuddy', 'attention': '重点关注'}, headers=H).json()
    gid = gs['groups'][0]['id']
    rows = {r['chat_id']: r for r in client.get('/api/chats').json()}
    assert rows['12']['attention'] == 'archive' and rows['12']['attention_source'] == '建议'
    r = client.put('/api/chats/11/group', json={'group_id': gid}, headers=H).json()
    assert (r['group'], r['attention'], r['confirmed']) == ('InfraBuddy', 'focus', True)
    r = client.put('/api/chats/11/group', json={'attention': 'archive'}, headers=H).json()
    assert r['attention_source'] == '单独设置'
    assert client.put('/api/chats/11/group', json={'group_id': 999}, headers=H).status_code == 400
    gs = client.put(f'/api/groups/{gid}', json={'attention': 'light', 'private': True}, headers=H).json()
    assert gs['groups'][0]['attention'] == 'light' and gs['groups'][0]['private'] and gs['groups'][0]['chats'] == 1
    assert client.delete(f'/api/groups/{gid}', headers=H).json()['groups'] == []


def test_jobs_and_run(client, monkeypatch):
    store = jobs.JobStore(Storage())
    store.finish(store.create('review', 's').id, '记了两条')
    d = client.get('/api/jobs').json()
    assert {j['name'] for j in d['jobs']} >= {'review', 'briefing', 'prune', 'organize'}
    assert d['runs'][0]['summary'] == '记了两条'
    monkeypatch.setattr(web, 'control_wait', lambda a: web._json({'ok': True, 'result': a}))
    assert client.post('/api/jobs/briefing/run', headers=H).json()['result'] == 'job:briefing'
    assert client.post('/api/jobs/nope/run', headers=H).status_code == 400


def test_proposals_apply_reject_undo(client, monkeypatch):
    from tests.test_organize import FakeFeishu
    fs = FakeFeishu()
    monkeypatch.setattr(web, '_lark', lambda: fs)
    p = organize.OrganizeStore(Storage()).submit({'mute': ['大群'], 'pin': [{'chat': '项目群'}]})
    rows = client.get('/api/proposals').json()
    assert [i['kind'] for i in rows[0]['items']] == ['pin', 'mute']
    r = client.post(f'/api/proposals/{p.id}', json={'action': 'apply', 'items': [2]}, headers=H).json()
    assert r['results'] == [{'seq': 2, 'result': '已执行'}] and fs.remind['12'] is False
    client.post(f'/api/proposals/{p.id}', json={'action': 'reject', 'items': [1]}, headers=H)
    assert [i['status'] for i in client.get('/api/proposals').json()[0]['items']] == ['rejected', 'applied']
    client.post(f'/api/proposals/{p.id}', json={'action': 'undo'}, headers=H)
    assert fs.remind['12'] is True
    ov = client.get('/api/overview').json()['lifecycle']
    assert ov['proposal'] is None and ov['attention']['archive'] >= 1


def test_memory_archive_meta_and_readonly_index(client, tmp_path):
    base = tmp_path / 'agent' / 'memory'
    (base / 'archive').mkdir(parents=True)
    (base / '人-陈威.md').write_text('---\ntype: 人\nname: 陈威\nupdated: 2026-11-03\n---\n同事', encoding='utf-8')
    (base / 'archive' / '事-旧.md').write_text('---\ntype: 事\nname: 旧\nupdated: 2026-01-01\n---\n', encoding='utf-8')
    (base / 'MEMORY.md').write_text('# 索引', encoding='utf-8')
    d = client.get('/api/memory').json()
    by = {f['name']: f for f in d['files']}
    assert by['人-陈威.md']['meta']['name'] == '陈威' and by['archive/事-旧.md']['archived'] and by['MEMORY.md']['generated']
    assert client.put('/api/memory/MEMORY.md', json={'content': 'x'}, headers=H).status_code == 400
    assert client.put('/api/memory/archive/事-旧.md', json={'content': '---\ntype: 事\n---\n'}, headers=H).status_code == 200
    assert client.put('/api/memory/../x.md', json={'content': 'x'}, headers=H).status_code in (400, 404)
    assert client.put('/api/memory/archive/../../x.md', json={'content': 'x'}, headers=H).status_code in (400, 404)
    assert not (tmp_path / 'x.md').exists() and not (tmp_path / 'agent' / 'x.md').exists()
    assert any(i['file'] == 'archive/事-旧.md' for i in client.get('/api/memory').json()['issues']) is False
