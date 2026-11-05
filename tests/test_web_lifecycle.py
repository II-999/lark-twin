"""网页: 会话分类、例行任务、整理方案、记忆(归档、字段、只读索引)、总览。"""
import time

import pytest
from starlette.testclient import TestClient

from larkx import groups, jobs
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
    gs = client.post('/api/groups', json={'name': '项目A', 'attention': '重点关注'}, headers=H).json()
    gid = gs['groups'][0]['id']
    old = client.get('/api/chats').json()                                              # 原来的接口不变
    assert set(old[0]) == {'chat_id', 'name', 'chat_type', 'updated_at'} and len(old[0]['updated_at']) > 19
    rows = {r['chat_id']: r for r in client.get('/api/chat-profiles').json()}
    assert rows['12']['attention'] == 'archive' and rows['12']['attention_source'] == '建议'
    r = client.put('/api/chats/11/group', json={'group_id': gid}, headers=H).json()
    assert (r['group'], r['attention'], r['confirmed']) == ('项目A', 'focus', True)
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
    assert {j['name'] for j in d['jobs']} >= {'review', 'profile', 'prune', 'consolidate'}
    assert d['runs'][0]['summary'] == '记了两条'
    monkeypatch.setattr(web, 'control_wait', lambda a: web._json({'ok': True, 'result': a}))
    assert client.post('/api/jobs/review/run', headers=H).json()['result'] == 'job:review'
    assert client.post('/api/jobs/nope/run', headers=H).status_code == 400




def test_overview_lifecycle(client):
    ov = client.get('/api/overview').json()['lifecycle']
    assert not ov['chat_groups'] and ov['attention'] == {} and {j['name'] for j in ov['jobs']} >= {'review'}
    assert client.get('/api/groups').json()['enabled'] is False


def test_disabled_job_cannot_run(tmp_path, monkeypatch, fake_client):
    import asyncio
    from tests.test_reliability import _daemon
    obj = _daemon(tmp_path, fake_client, monkeypatch)
    assert '没有打开' in asyncio.run(obj.control('job:prune')) and obj.inbox.qsize() == 0
