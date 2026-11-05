"""回填历史: 只拉核对位置以下、到 N 天前或空批就停、上限、断点续拉、不触发不标已读、会话时间按消息时间;
回填完接着画像和初始化记忆(分批)。"""
import asyncio
import time
from datetime import datetime

import pytest

from larkx import backfill, jobs
from larkx.storage import Storage

OWNER = '100'
DAY = 86400


class HistoryClient:
    """会话 -> {position: 消息}; 记下拉过的位置。"""

    def __init__(self, chats: dict, names: dict, feed=None):
        self.chats, self.names, self.pulled = chats, names, []
        self.feed = feed if feed is not None else list(chats)
        self.auth = type('A', (), {'user_id': OWNER})()
        self.read = 0

    def feed_chats(self, since, max_pages=20):
        return list(self.feed)

    def api(self, name, data=None):
        assert name == 'chats.PullChatsByIdsRequest'
        return {'chats': {c: {'type': 2, 'name': self.names.get(c, ''),
                              'lastMessagePosition': max(self.chats[c] or [0])} for c in data['chatIds']}}

    def pull_history(self, cid, positions):
        positions = list(positions)
        self.pulled.append((cid, positions[0], positions[-1]))
        return [dict(self.chats[cid][p]) for p in positions if p in self.chats[cid]]

    def get_user_name(self, uid, cid):
        return '我' if uid == OWNER else f'用户{uid}'

    def mark_read(self, *a):
        self.read += 1


def history(cid, n, step_hours=10, start_pos=1):
    now = time.time()
    return {p: {'msg_id': f'{cid}-{p}', 'chat_id': cid, 'chat_type': 2, 'position': p, 'from_id': '200' if p % 3 else OWNER,
                'content': f'消息{p}', 'create_time': int(now - (start_pos + n - 1 - p) * step_hours * 3600)}
            for p in range(start_pos, start_pos + n)}


@pytest.fixture
def st(tmp_path):
    return Storage(f'sqlite:///{tmp_path}/b.db')


def test_backfill_below_mark_until_since(st):
    client = HistoryClient({'g1': history('g1', 200), 'g2': history('g2', 10)}, {'g1': '项目群', 'g2': '小群'})
    st.catchup_advance('g1', 190)                                     # 补收已经核对到 190, 之上不归回填管
    saves = []
    res = backfill.Backfill(client, st, OWNER, 30, progress=saves.append, sleep=lambda s: None).run()
    assert res['done'] and res['chats'] == 2
    pos = sorted(m.position for m in st.get_messages('g1', limit=1000))
    assert max(pos) == 190 and min(pos) >= 200 - 30 * 24 // 10 - 1   # 只到 30 天前
    assert len(st.get_messages('g2', limit=100)) == 10                # 整个会话都拉完(位置到 1 停)
    assert saves and saves[-1]['done']
    assert client.read == 0
    chat = {c.chat_id: c for c in st.list_chats(10)}
    assert chat['g1'].name == '项目群' and chat['g1'].updated_at < datetime.now().replace(microsecond=0)
    assert all(m.direction == ('out' if m.sender_id == OWNER else 'in') for m in st.get_messages('g1', limit=1000))


def test_low_value_and_caps(st, monkeypatch):
    monkeypatch.setattr(backfill, 'PER_CHAT', 60)
    client = HistoryClient({'t1': history('t1', 300, step_hours=1), 'g1': history('g1', 300, step_hours=1)},
                           {'t1': 'TaeBuddy测试群', 'g1': '项目群'})
    backfill.Backfill(client, st, OWNER, 30, sleep=lambda s: None).run()
    assert len(st.get_messages('t1', limit=1000)) == 3 * 24           # 测试群只拉 3 天
    assert 60 <= len(st.get_messages('g1', limit=1000)) < 110         # 到单会话上限就停(按批取整)


def test_resume_from_checkpoint(st):
    client = HistoryClient({'g1': history('g1', 120, step_hours=1)}, {'g1': '项目群'})
    saved = {}
    stop = iter([False, True])
    res = backfill.Backfill(client, st, OWNER, 30, progress=saved.update, sleep=lambda s: None).run(
        should_stop=lambda: next(stop, True))
    assert res['stopped'] and not saved['done'] and 0 < saved['messages'] < 120
    client.pulled.clear()
    res = backfill.Backfill(client, st, OWNER, 30, saved=dict(saved), sleep=lambda s: None).run()
    assert res['done'] and len(st.get_messages('g1', limit=1000)) == 120
    assert client.pulled[0][2] < 120                                  # 从断点往下接着拉


@pytest.mark.parametrize('grouped', [True, False])
def test_daemon_chain_backfill_profile_bootstrap(tmp_path, monkeypatch, fake_client, grouped):
    from tests.test_reliability import _daemon
    obj = _daemon(tmp_path, fake_client, monkeypatch)
    obj.cfg.update(memory_review_hour=23 if datetime.now().hour < 23 else -1, memory_consolidate_weekday=-1,
                   backfill_days=30, chat_groups=grouped)
    if obj.cfg['memory_review_hour'] < 0:
        pytest.skip('23 点后跑不了这个用例')
    obj.auth.user_id = OWNER
    chats = {f'g{i}': history(f'g{i}', 20) for i in range(10)}
    hc = HistoryClient(chats, {c: f'项目{c}' for c in chats})
    obj.client = hc
    hc.api = (lambda orig: (lambda name, data=None: {'shortcuts': []} if name == 'feed.PullShortcutsRequest'
                            else orig(name, data)))(hc.api)
    hc.is_bot = lambda uid: False
    monkeypatch.setattr(backfill, 'PAUSE', 0)
    store = obj.maintenance.store

    async def settle():
        await obj.maintenance.tick()
        while obj.maintenance._tasks:
            await asyncio.gather(*list(obj.maintenance._tasks))
    asyncio.run(settle())
    assert store.last('backfill') is None                           # 第一轮补收还没结束, 先不回填
    obj._feed_since = time.time()
    asyncio.run(settle())
    assert store.get_state('backfill')['done'] and store.last('backfill').status == 'ok'
    if grouped:                                                       # 打开会话分类: 回填完接着画像
        assert store.last('profile').status == 'ok' and store.last('profile').trigger == 'chain'
    else:                                                             # 没打开: 不画像, 直接按本人发言挑会话
        assert store.last('profile') is None
    asyncio.run(settle())
    ev = obj.inbox.get_nowait()                                       # 每个群本人都发过言, 10 个分两批
    assert ev.ref.startswith('job:bootstrap#') and '第 1/2 批' in ev.prompt and '--since 30d' in ev.prompt
    assert ('重点关注的会话' if grouped else '本人发言最多的会话') in ev.prompt
    asyncio.run(obj._handle(ev))
    asyncio.run(settle())
    ev2 = obj.inbox.get_nowait()
    assert '第 2/2 批' in ev2.prompt and '最后一批' in ev2.prompt
    asyncio.run(obj._handle(ev2))
    asyncio.run(settle())
    assert store.get_state('bootstrap')['done'] and obj.inbox.qsize() == 0


def test_backfill_off_by_default():
    from larkx.config import DEFAULTS
    assert jobs.get('backfill').when(dict(DEFAULTS)) is None and jobs.get('bootstrap').when(dict(DEFAULTS)) is None


def test_agent_cannot_backfill():
    from larkx.agent.hooks import check_tool_use
    assert check_tool_use('Bash', {'command': 'larkx backfill --days 3'})


def test_doctor_backfill(tmp_path):
    from larkx import doctor
    from larkx.config import DEFAULTS
    st = Storage(f'sqlite:///{tmp_path}/d.db')
    assert doctor.check_backfill(dict(DEFAULTS), st)['level'] == 'ok'                   # 默认关
    cfg = dict(DEFAULTS, backfill_days=30)
    assert doctor.check_backfill(cfg, st)['level'] == 'warn'
    store = jobs.JobStore(st)
    store.put_state('backfill', {'order': ['a', 'b'], 'chats_done': 2, 'messages': 30, 'done': True})
    store.put_state('bootstrap', {'chats': ['a'], 'next': 1, 'done': True})
    c = doctor.check_backfill(cfg, st)
    assert c['level'] == 'ok' and '2/2 个会话, 30 条' in c['detail'] and '记忆已初始化' in c['detail']
    assert doctor.check_backfill(dict(cfg, backfill_days=0), st)['level'] == 'ok'


def _maint(tmp_path, monkeypatch, fake_client, **cfg):
    from tests.test_reliability import _daemon
    obj = _daemon(tmp_path, fake_client, monkeypatch)
    obj.cfg.update(memory_review_hour=4, memory_consolidate_weekday=-1, backfill_days=30, **cfg)
    obj.auth.user_id = OWNER
    return obj, obj.maintenance, obj.maintenance.store


def test_bootstrap_skips_batch_that_keeps_failing(tmp_path, monkeypatch, fake_client):
    """某一批重试用完(包括被重启打断): 跳过它接着下一批, 不会永远卡住。"""
    obj, m, store = _maint(tmp_path, monkeypatch, fake_client)
    store.put_state('backfill', {'done': True, 'finished_at': time.time()})
    store.put_state('bootstrap', {'chats': [f'c{i}' for i in range(20)], 'next': 0})       # 3 批
    job = jobs.get('bootstrap')
    assert m._once_slot(job) == 'batch-1'
    for i in (1, 2):
        r = store.create('bootstrap', 'batch-1', attempt=i)
        store._set(r.id, status='interrupted')
    assert m._once_slot(job) == 'batch-2' and store.get_state('bootstrap')['skipped'] == [1]
    store.create('bootstrap', 'batch-2', status='skipped')              # 本人 /stop 打断了这一批
    store.finish(store.create('bootstrap', 'batch-3').id)
    assert m._once_slot(job) == 'batch-3'                     # 第 3 批已完成, 由 on_turn_done 推进
    bs = store.get_state('bootstrap')
    bs['next'] = 3
    store.put_state('bootstrap', bs)
    assert m._once_slot(job) is None and store.get_state('bootstrap')['done']
    from larkx import doctor
    assert '跳过了第 1、2 批' in doctor.check_backfill(obj.cfg, obj.storage)['detail']


def test_backfill_gets_new_attempts_each_day_and_pauses_on_stop(tmp_path, monkeypatch, fake_client):
    obj, m, store = _maint(tmp_path, monkeypatch, fake_client)
    obj._feed_since = time.time()
    key = m._once_slot(jobs.get('backfill'))
    assert key == 'once-' + datetime.now().strftime('%Y-%m-%d')            # 按天给时段, 第二天接着回填
    chats = {f'g{i}': history(f'g{i}', 20) for i in range(3)}
    obj.client = HistoryClient(chats, {c: c for c in chats})
    monkeypatch.setattr(backfill, 'PAUSE', 0)
    m.stop()                                                                # 停服务: 第一批之前就停
    run = store.create('backfill', key)
    asyncio.run(m._run_local(jobs.get('backfill'), run))
    assert store.get(run.id).status == jobs.PAUSED and not [t for c, t in fake_client.sent if '失败' in t]
    assert store.due(jobs.get('backfill'), obj.cfg, datetime.now(), cur=(key, datetime.now()))[0] == 'run'   # 暂停不算次数


def test_bootstrap_final_pass_when_last_batch_skipped(tmp_path, monkeypatch, fake_client):
    obj, m, store = _maint(tmp_path, monkeypatch, fake_client)
    store.put_state('backfill', {'done': True, 'finished_at': time.time()})
    store.put_state('bootstrap', {'chats': [f'c{i}' for i in range(10)], 'next': 1})       # 2 批, 第 1 批已完成
    store.create('bootstrap', 'batch-2', status='skipped')                                 # 最后一批被跳过
    job = jobs.get('bootstrap')
    assert m._once_slot(job) == 'final'
    from larkx.daemon import prompts
    assert '初始化记忆收尾' in prompts.bootstrap(obj.cfg, obj.storage, datetime.now(), OWNER)
    from larkx.agent import TurnResult
    run = store.create('bootstrap', 'final')
    m.on_turn_done(type('E', (), {'run_id': run.id, 'chat_id': 'self'})(), TurnResult('success', False, 'ok', 1.0))
    assert m._once_slot(job) is None and store.get_state('bootstrap')['done']


def test_backfill_cancelled_on_sigterm_is_paused(tmp_path, monkeypatch, fake_client):
    obj, m, store = _maint(tmp_path, monkeypatch, fake_client)
    monkeypatch.setattr(type(m), '_job_backfill', lambda self, run: time.sleep(0.5), raising=False)
    run = store.create('backfill', 'once-x')

    async def go():
        t = asyncio.create_task(m._run_local(jobs.get('backfill'), run))
        await asyncio.sleep(0.05)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
    asyncio.run(go())
    assert store.get(run.id).status == jobs.PAUSED
