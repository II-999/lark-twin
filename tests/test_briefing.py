"""每日简报: 数据(未回复、标记、重点动态、提醒)、周一周报、只发本人私聊、没发出时兜底、配置检查。"""
import asyncio
import time
from datetime import datetime, timedelta

import pytest

from larkx import digest, groups, jobs
from larkx.config import DEFAULTS
from larkx.storage import Storage

OWNER = '100'


def _msg(st, mid, chat, sender, content='hi', chat_type=2, name='', age=3600, at_me=False):
    st.save_message({'msg_id': mid, 'chat_id': chat, 'chat_type': chat_type, 'from_id': sender, 'content': content,
                     'create_time': int(time.time() - age),
                     'content_data': {'_larkx_mention_ids': [OWNER]} if at_me else {}},
                    sender_name='我' if sender == OWNER else '张三', chat_name=name,
                    direction='out' if sender == OWNER else 'in')


@pytest.fixture
def st(tmp_path):
    s = Storage(f'sqlite:///{tmp_path}/b.db')
    _msg(s, 'o1', 'g1', OWNER, '我在项目群', name='项目群', age=60)
    _msg(s, 'a1', 'g1', '200', '@我 帮忙看下 MR', name='项目群', at_me=True)
    _msg(s, 'p1', 'p1', '300', '明天有空吗', chat_type=1, name='李四')
    _msg(s, 'b1', 'big', '200', '@我 大群里的', name='大群', at_me=True)
    _msg(s, 'l1', 'lg', '200', '@我 告警', name='告警群', at_me=True)
    cs = groups.ChatStore(s)
    cs.save_profile('g1', OWNER, name='项目群', chat_type=2, msgs=2, owner_msgs=5)
    cs.save_profile('p1', OWNER, name='李四', chat_type=1, peer_id='300', peer_bot=0, msgs=1, owner_msgs=4)
    cs.save_profile('big', OWNER, name='大群', chat_type=2, members=999, msgs=1)
    cs.save_profile('lg', OWNER, name='告警群', chat_type=2, msgs=1, feishu_muted=1)
    s.add_task(kind='task', due_at=int(time.time() + 3600), prompt='提醒回复王五', chat_id='self')
    return s


class FlagClient:
    def api(self, name, data=None):
        return {'items': [{'itemId': 'a1'}]} if name == 'feed.PullFlagsRequest' else {}


def test_briefing_data(st):
    now = datetime.now()
    if now.weekday() == 0:
        now = now + timedelta(days=1)  # 用非周一
    d = digest.briefing_data(st, OWNER, now=now, client=FlagClient())
    assert not d['weekly'] and d['window'] == '过去 24 小时'
    assert {u['chat_name'] for u in d['unreplied']} == {'李四', '告警群'}   # 项目群本人后来回过; 大群只存不看
    assert [f['msg_id'] for f in d['flags']] == ['a1']                         # 标记的照列, 回没回过都算
    assert [r['chat_id'] for r in d['focus']] == ['g1', 'p1'] or [r['chat_id'] for r in d['focus']] == ['p1', 'g1']
    assert [r['chat_id'] for r in d['light_related']] == ['lg']
    assert d['tasks'][0]['prompt'] == '提醒回复王五'
    text = digest.render(d)
    assert '找你还没回的' in text and '明天有空吗' in text and '〔免打扰群〕' in text and '大群里的' not in text


def test_flags_listed(st):
    st.save_message({'msg_id': '777', 'chat_id': 'g1', 'chat_type': 2, 'from_id': '200', 'content': '记得改',
                     'create_time': int(time.time())}, sender_name='张三', chat_name='项目群')

    class C:
        def api(self, name, data=None):
            return {'items': [{'itemId': 777}]}
    d = digest.briefing_data(st, OWNER, client=C())
    assert d['flags'][0]['content'] == '记得改' and '你标记的待处理' in digest.render(d)


def test_weekly_on_monday(st):
    monday = datetime.now() - timedelta(days=datetime.now().weekday())
    monday = monday.replace(hour=9, minute=0)
    if monday > datetime.now():
        monday -= timedelta(days=7)
    d = digest.briefing_data(st, OWNER, now=monday + timedelta(hours=0))
    assert d['weekly'] and d['window'] == '过去 7 天'
    from larkx.daemon import prompts
    assert '上周回顾' in prompts.briefing({}, st, monday, OWNER)


def test_briefing_time():
    assert jobs.briefing_time({'briefing_at': '09:30'}) == (9, 30)
    assert jobs.briefing_time({'briefing_at': 'off'}) is None
    with pytest.raises(ValueError):
        jobs.briefing_time({'briefing_at': '9点'})


def _daemon(tmp_path, monkeypatch, fake_client):
    from tests.test_reliability import _daemon as make
    obj = make(tmp_path, fake_client, monkeypatch)
    obj.cfg.update(memory_review_hour=-1, memory_consolidate_weekday=-1, backfill_days=0, briefing_at='00:00')
    return obj


def test_briefing_is_report_to_owner_chat(tmp_path, monkeypatch, fake_client):
    obj = _daemon(tmp_path, monkeypatch, fake_client)
    if datetime.now().hour >= 3:
        obj.cfg['briefing_at'] = (datetime.now() - timedelta(hours=1)).strftime('%H:%M')
    asyncio.run(obj.maintenance.tick())
    ev = obj.inbox.get_nowait()
    assert (ev.kind, ev.chat_id) == ('report', 'self')
    assert ev.prompt.startswith('<report kind="briefing">') and "larkx send self -m - <<'EOF'" in ev.prompt


def test_briefing_fallback_when_not_sent(tmp_path, monkeypatch, fake_client):
    from larkx.agent import TurnResult
    obj = _daemon(tmp_path, monkeypatch, fake_client)
    run = obj.maintenance.store.create('briefing', 'x')
    ev = type('E', (), {'run_id': run.id, 'chat_id': 'self'})()
    obj.maintenance.on_turn_done(ev, TurnResult('success', False, '写好了', 1.0))   # 说写好了, 审计里却没有发送
    assert obj.maintenance.store.get(run.id).status == 'failed'
    assert any('工作简报' in t and '数据版' in t for c, t in fake_client.sent if c == 'self')
    run2 = obj.maintenance.store.create('briefing', 'y')
    obj.storage.log_action(actor='agent', action='send', chat_id='self', content='简报', turn_ref=run2.ref)
    n = len(fake_client.sent)
    obj.maintenance.on_turn_done(type('E', (), {'run_id': run2.id, 'chat_id': 'self'})(), TurnResult('success', False, 'ok', 1.0))
    assert obj.maintenance.store.get(run2.id).status == 'ok' and len(fake_client.sent) == n


def test_doctor_briefing(monkeypatch):
    from larkx import doctor, notify
    monkeypatch.setattr(notify, '_load', lambda: {})
    assert doctor.check_briefing(dict(DEFAULTS))['level'] == 'warn'
    monkeypatch.setattr(notify, '_load', lambda: {'owner_chat': 'self'})
    assert doctor.check_briefing(dict(DEFAULTS))['level'] == 'ok'
    assert doctor.check_briefing(dict(DEFAULTS, briefing_at='9点'))['level'] == 'fail'


def test_new_chats_listed(st):
    from larkx import jobs as J
    J.JobStore(st).put_state('backfill', {'done': True, 'finished_at': time.time() - 3 * 86400})
    d = digest.briefing_data(st, OWNER)
    assert {c['name'] for c in d['new_chats']} == {'项目群', '李四', '大群', '告警群'}
    groups.ChatStore(st).assign(['g1'], attention='light')                 # 确认过的不算新会话
    d = digest.briefing_data(st, OWNER)
    assert '项目群' not in {c['name'] for c in d['new_chats']} and '新加入的会话' in digest.render(d)
