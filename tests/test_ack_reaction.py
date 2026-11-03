"""触发时先回一个表情: 网页"设置"页打开后, daemon 在触发数字人的那条消息上加一个 OK 表情, 再照常排队。默认关。"""
import asyncio
import threading
import time
from datetime import datetime

import pytest
from starlette.testclient import TestClient

from larkx.storage import Storage
from larkx.storage.models import Action
from larkx.web import app as web
from test_reliability import _daemon

ME, OTHER, LISTED = '100', '200', '300'


class Session:
    """能停、能清空的假会话(/ai /stop、/ai /clear 用)。"""

    def __init__(self):
        self.prompts, self.events, self.interrupts, self.resets = [], [], 0, 0

    async def interrupt(self):
        self.interrupts += 1
        return False

    async def reset(self):
        self.resets += 1


def daemon(tmp_path, monkeypatch, fake_client, scope=None):
    """scope 给了就在设置里打开(owner / all), 不给就是默认(关)。"""
    monkeypatch.delenv('LARKX_ACTOR', raising=False)
    obj = _daemon(tmp_path, fake_client, monkeypatch)
    obj.session = Session()
    obj.storage.whitelist_add(LISTED, '白名单里的人')
    if scope:
        obj.storage.set_ack_reaction(True, scope)
    return obj


def msg(content='/ai 在吗', from_id=ME, msg_id='m1', chat_id='p1', chat_type=1, **kw):
    return {'msg_id': msg_id, 'chat_id': chat_id, 'chat_type': chat_type, 'from_id': from_id, 'content': content,
            'create_time': int(time.time()), **kw}


async def receive(obj, *msgs):
    """逐条交给 daemon, 等后台加表情的任务都做完。"""
    for m in msgs:
        await obj.on_message(m, '会话', '某人')
    await asyncio.gather(*list(obj._ack_tasks))


def acks(st):
    s = st.Session()
    try:
        return s.query(Action).filter(Action.action == 'ack_reaction').order_by(Action.id).all()
    finally:
        s.close()


def queued(obj):
    out = []
    while not obj.inbox.empty():
        out.append(obj.inbox.get_nowait().ref)
    return out


# 本人的 /ai、白名单里的人私聊唤起、白名单里的人在群里 @ 本人唤起(话题里的回复也加在那条本身)
TRIGGERS = (msg(msg_id='o1'), msg(from_id=LISTED, msg_id='g1'),
            msg(from_id=LISTED, msg_id='g2', chat_id='grp', chat_type=2, at_me=True, anchor='root9'),
            msg('/ai 帮我看看', msg_id='o2', chat_id='grp', chat_type=2))


# ---- 默认关 ----

def test_off_by_default(tmp_path, monkeypatch, fake_client):
    obj = daemon(tmp_path, monkeypatch, fake_client)
    assert obj.storage.ack_reaction() == {'enabled': False, 'scope': 'owner'}
    asyncio.run(receive(obj, *TRIGGERS))
    assert fake_client.reactions == [] and acks(obj.storage) == []
    assert queued(obj) == ['o1', 'g1', 'g2', 'o2']


# ---- 范围 ----

def test_owner_scope(tmp_path, monkeypatch, fake_client):
    """只对本人: 本人的 /ai 加, 白名单里的人唤起的不加; 都照常排队。"""
    obj = daemon(tmp_path, monkeypatch, fake_client, 'owner')
    asyncio.run(receive(obj, *TRIGGERS))
    assert fake_client.reactions == [('o1', 'OK'), ('o2', 'OK')]
    assert queued(obj) == ['o1', 'g1', 'g2', 'o2']
    rows = acks(obj.storage)
    assert [(r.actor, r.chat_id, r.msg_id, r.content, r.ok, r.turn_kind, r.turn_ref) for r in rows] == [
        ('daemon', 'p1', 'o1', 'OK', 1, 'message', 'o1'), ('daemon', 'grp', 'o2', 'OK', 1, 'message', 'o2')]
    # 不是发出的消息: 不算进数字人发出的条数和发送上限, 也没发任何消息
    assert obj.storage.count_actions('agent', ('send', 'forward'), datetime(2000, 1, 1)) == 0
    assert fake_client.sent == []


def test_all_scope(tmp_path, monkeypatch, fake_client):
    obj = daemon(tmp_path, monkeypatch, fake_client, 'all')
    asyncio.run(receive(obj, *TRIGGERS))
    assert fake_client.reactions == [('o1', 'OK'), ('g1', 'OK'), ('g2', 'OK'), ('o2', 'OK')]
    assert queued(obj) == ['o1', 'g1', 'g2', 'o2']


def test_setting_takes_effect_without_restart(tmp_path, monkeypatch, fake_client):
    """设置每条消息现查: 网页上改了, 下一条就按新的来。"""
    obj = daemon(tmp_path, monkeypatch, fake_client)
    st = Storage(obj.storage.url)  # 网页那边是另一个进程, 各开各的连接
    asyncio.run(receive(obj, msg(msg_id='a')))
    st.set_ack_reaction(enabled=True)
    asyncio.run(receive(obj, msg(msg_id='b'), msg(from_id=LISTED, msg_id='c')))
    st.set_ack_reaction(scope='all')
    asyncio.run(receive(obj, msg(from_id=LISTED, msg_id='d')))
    st.set_ack_reaction(enabled=False)
    asyncio.run(receive(obj, msg(msg_id='e')))
    assert fake_client.reactions == [('b', 'OK'), ('d', 'OK')]
    assert queued(obj) == ['a', 'b', 'c', 'd', 'e']


def test_command_with_body_reacts(tmp_path, monkeypatch, fake_client):
    """/ai /clear 后面带了正文的, 正文照常交给数字人, 也加表情。"""
    obj = daemon(tmp_path, monkeypatch, fake_client, 'owner')
    obj._turns_since_flush = 0  # 上次整理记忆后没聊过: /clear 直接重置(聊过的先整理记忆, 见 test_memory_tools)
    asyncio.run(receive(obj, msg('/ai /clear 新问题: 今天有什么会')))
    assert obj.session.resets == 1
    assert fake_client.reactions == [('m1', 'OK')] and queued(obj) == ['m1']


# ---- 不加表情 ----

@pytest.mark.parametrize('content', ['/ai /stop', '/ai /clear', '/ai /takeover 1h 约个会', '/ai /release'])
def test_control_commands_dont_react(tmp_path, monkeypatch, fake_client, content):
    obj = daemon(tmp_path, monkeypatch, fake_client, 'all')
    obj._turns_since_flush = 0  # /clear 不排整理记忆, 队列里什么都没有
    asyncio.run(receive(obj, msg(content, chat_id='p9')))
    assert fake_client.reactions == [] and queued(obj) == [] and acks(obj.storage) == []


def test_takeover_denied_and_old_dont_react(tmp_path, monkeypatch, fake_client):
    """接管中的会话(对方每句话都交给数字人)、没权限被拒的、超过 LARKX_INBOX_MAX_AGE 的旧消息都不加, 范围是"所有"也不加。"""
    obj = daemon(tmp_path, monkeypatch, fake_client, 'all')
    obj.storage.takeover_start('tk', int(time.time()) + 3600, '', 'owner')
    old = int(time.time()) - 3 * 3600
    asyncio.run(receive(obj,
                        msg('你好', from_id=OTHER, msg_id='t1', chat_id='tk'),          # 接管: 照常排队
                        msg('/ai 在吗', from_id=OTHER, msg_id='d1', chat_id='p2'),      # 没权限(不在白名单里): 回一句, 不排队
                        msg(msg_id='o1', create_time=old),                              # 本人很久以前的 /ai
                        msg(from_id=LISTED, msg_id='o2', create_time=old)))
    assert queued(obj) == ['t1']
    assert len(fake_client.replies) == 1                                                # 没权限回复
    assert fake_client.reactions == [] and acks(obj.storage) == []


def test_reacts_once(tmp_path, monkeypatch, fake_client):
    """同一条重复推送、补收又收到时不再加, 也不再排队; daemon 重启后接着处理的也不再加。"""
    obj = daemon(tmp_path, monkeypatch, fake_client, 'owner')
    asyncio.run(receive(obj, msg(), msg()))
    assert fake_client.reactions == [('m1', 'OK')] and queued(obj) == ['m1']
    obj._restore_inbox()
    asyncio.run(receive(obj))
    assert queued(obj) == ['m1'] and fake_client.reactions == [('m1', 'OK')] and len(acks(obj.storage)) == 1


# ---- 加表情出错、慢都不影响排队 ----

def test_reaction_failure_still_queues(tmp_path, monkeypatch, fake_client):
    obj = daemon(tmp_path, monkeypatch, fake_client, 'owner')
    fake_client.reaction_error = RuntimeError('400 Bad Request')
    asyncio.run(receive(obj, msg()))
    assert queued(obj) == ['m1'] and fake_client.reactions == [('m1', 'OK')]
    [row] = acks(obj.storage)
    assert row.ok == 0 and '400' in row.error


def test_broken_setting_still_queues(tmp_path, monkeypatch, fake_client):
    """读设置出错也照常排队, 不加。"""
    obj = daemon(tmp_path, monkeypatch, fake_client, 'owner')

    def boom():
        raise RuntimeError('database is locked')
    monkeypatch.setattr(obj.storage, 'ack_reaction', boom)
    asyncio.run(receive(obj, msg()))
    assert queued(obj) == ['m1'] and fake_client.reactions == []


def test_reaction_runs_in_background(tmp_path, monkeypatch, fake_client):
    """加表情在线程里做, 不等它: 请求卡住时消息也已经排上队了。"""
    obj = daemon(tmp_path, monkeypatch, fake_client, 'owner')
    started, release = threading.Event(), threading.Event()

    def slow(msg_id, reaction_type='OK'):
        started.set()
        release.wait(5)
        fake_client.reactions.append((msg_id, reaction_type))
        return 1

    fake_client.add_reaction = slow

    async def run():
        await obj.on_message(msg(), '会话', '某人')
        n = obj.inbox.qsize()
        await asyncio.to_thread(started.wait, 5)
        done_before = list(fake_client.reactions)
        release.set()
        await asyncio.gather(*obj._ack_tasks)
        return n, done_before

    n, done_before = asyncio.run(run())
    assert n == 1 and done_before == [] and fake_client.reactions == [('m1', 'OK')]


# ---- 存储 ----

def test_storage_setting(tmp_path, monkeypatch):
    st = Storage(f'sqlite:///{tmp_path}/s.db')
    assert st.ack_reaction() == {'enabled': False, 'scope': 'owner'}
    assert st.set_ack_reaction(scope='all') == {'enabled': False, 'scope': 'all'}
    assert st.set_ack_reaction(enabled=True) == {'enabled': True, 'scope': 'all'}
    assert Storage(st.url).ack_reaction() == {'enabled': True, 'scope': 'all'}
    for bad in ({'enabled': 'true'}, {'enabled': 1}, {'scope': 'everyone'}):
        with pytest.raises(ValueError):
            st.set_ack_reaction(**bad)
    st.setting_set('ack_reaction', {'enabled': 'yes', 'scope': 'x'})  # 库里的值不对时按默认(关)
    assert st.ack_reaction() == {'enabled': False, 'scope': 'owner'}
    # 数字人改不了
    monkeypatch.setenv('LARKX_ACTOR', 'agent')
    with pytest.raises(PermissionError):
        st.set_ack_reaction(enabled=True)
    assert st.ack_reaction()['enabled'] is False


# ---- 网页 ----

@pytest.fixture
def wclient(tmp_path, monkeypatch):
    monkeypatch.setattr(web, 'TOKEN_PATH', tmp_path / 'web_token')
    monkeypatch.delenv('LARKX_ACTOR', raising=False)
    monkeypatch.delenv('LARKX_INBOX_MAX_AGE', raising=False)
    return TestClient(web.create_app())


H = {'X-Larkx': '1'}


def test_web_settings_need_login_and_header(wclient):
    c = wclient
    assert c.get('/api/settings').status_code == 401
    assert c.put('/api/settings', json={'ack_reaction': {'enabled': True}}, headers=H).status_code == 401
    c.post('/login', json={'token': 'wrong'})
    assert c.put('/api/settings', json={'ack_reaction': {'enabled': True}}, headers=H).status_code == 401
    c.post('/login', json={'token': web.get_token()})
    assert c.put('/api/settings', json={'ack_reaction': {'enabled': True}}).status_code == 403  # 没有 X-Larkx
    assert c.post('/api/settings', json={'ack_reaction': {'enabled': True}}, headers=H).status_code == 405
    assert Storage().ack_reaction()['enabled'] is False


def test_web_settings_flow(wclient, monkeypatch):
    c = wclient
    c.post('/login', json={'token': web.get_token()})
    assert c.get('/api/settings').json() == {'ack_reaction': {'enabled': False, 'scope': 'owner', 'reaction': 'OK'},
                                             'inbox_max_age': 7200}
    r = c.put('/api/settings', json={'ack_reaction': {'enabled': True}}, headers=H).json()
    assert r['ok'] and r['ack_reaction'] == {'enabled': True, 'scope': 'owner', 'reaction': 'OK'}
    r = c.put('/api/settings', json={'ack_reaction': {'scope': 'all'}}, headers=H).json()
    assert r['ack_reaction'] == {'enabled': True, 'scope': 'all', 'reaction': 'OK'}
    assert Storage().ack_reaction() == {'enabled': True, 'scope': 'all'}  # daemon 读的就是这份
    c.put('/api/settings', json={'ack_reaction': {'scope': 'all'}}, headers=H)  # 没变的不记动态
    c.put('/api/settings', json={'ack_reaction': {'enabled': False, 'scope': 'owner'}}, headers=H)
    # 不合法的
    for body in ({'ack_reaction': {'enabled': 'yes'}}, {'ack_reaction': {'scope': 'everyone'}}, {'ack_reaction': {'emoji': 'THUMBSUP'}},
                 {'ack_reaction': {}}, {'ack_reaction': 'on'}, {'mark_read': True}, {}):
        r = c.put('/api/settings', json=body, headers=H)
        assert r.status_code == 400, body
    assert Storage().ack_reaction() == {'enabled': False, 'scope': 'owner'}
    # 改动记进动态(新的在前), 不算发出的消息
    acts = [a for a in c.get('/api/activity').json() if a['action'] == 'setting']
    assert [(a['actor'], a['content']) for a in acts] == [
        ('human', '网页设置: 触发时先回 OK 表情关闭, 范围: 只对本人自己发的 /ai 消息'),
        ('human', '网页设置: 触发时先回 OK 表情打开, 范围: 所有触发数字人的消息'),
        ('human', '网页设置: 触发时先回 OK 表情打开, 范围: 只对本人自己发的 /ai 消息')]
    # 存储层的保护: 就算从数字人的进程里调到这个接口也改不了
    monkeypatch.setenv('LARKX_ACTOR', 'agent')
    r = c.put('/api/settings', json={'ack_reaction': {'enabled': True}}, headers=H)
    assert r.status_code == 403 and Storage().ack_reaction()['enabled'] is False
