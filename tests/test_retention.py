"""按关注度清理消息、保护期、旧记录整理、prune 命令和权限。"""
import argparse
import json
import time
from datetime import datetime, timedelta

import pytest

from larkx import cli, groups, retention
from larkx.config import DEFAULTS
from larkx.storage import Storage
from larkx.storage.models import ChatProfile, Control, InboxItem, JobRun, Task

DAY = retention.DAY
OWNER = '100'


@pytest.fixture
def st(tmp_path):
    return Storage(f'sqlite:///{tmp_path}/r.db')


def _msg(st, mid, chat, age_days, name=None):
    st.save_message({'msg_id': mid, 'chat_id': chat, 'chat_type': 2, 'from_id': '200', 'content': 'x',
                     'create_time': int(time.time() - age_days * DAY)}, chat_name=name or chat)


def _ids(st, chat):
    return sorted(m.msg_id for m in st.get_messages(chat, limit=100))


def _age_profile(st, chat, days):
    s = st.Session()
    p = s.get(ChatProfile, chat)
    p.created_at = datetime.now() - timedelta(days=days)
    s.commit()
    s.close()


def _setup(st):
    cs = groups.ChatStore(st)
    for chat in ('focus', 'light', 'big', 'fresh', 'mine', 'none'):
        for d in (1, 40, 100):
            _msg(st, f'{chat}-{d}', chat, d)
    cs.save_profile('focus', OWNER, name='项目群', chat_type=2, msgs=3, owner_msgs=9)
    cs.save_profile('light', OWNER, name='一般群', chat_type=2, msgs=3)
    cs.save_profile('big', OWNER, name='大群', chat_type=2, members=999, msgs=3)       # 建议只存不看
    cs.save_profile('fresh', OWNER, name='新大群', chat_type=2, members=999, msgs=3)   # 建议只存不看, 还在保护期
    g = cs.create_group('技术圈', 'archive')
    cs.assign(['mine'], group_id=g.id)                                                # 本人确认的只存不看
    for c in ('focus', 'light', 'big'):
        _age_profile(st, c, 10)
    return cs


def test_defaults_delete_nothing(st):
    _setup(st)
    assert retention.plan(dict(DEFAULTS), st) == []                                   # 默认一直保留


A30 = dict(DEFAULTS, retention_archive_days=30)


def test_plan_by_attention(st):
    _setup(st)
    steps = retention.plan(A30, st)
    assert len(steps) == 1 and sorted(steps[0][2]) == ['big', 'mine']               # 新大群还在保护期
    res = retention.run(A30, st)
    assert res['messages'][0][1] == 4
    assert _ids(st, 'big') == ['big-1'] and _ids(st, 'mine') == ['mine-1']
    assert len(_ids(st, 'fresh')) == len(_ids(st, 'focus')) == len(_ids(st, 'light')) == len(_ids(st, 'none')) == 3
    assert st.get_chat_name('big') == 'big'                                            # 会话列表保留


def test_light_days_and_off(st):
    _setup(st)
    cfg = dict(DEFAULTS, retention_light_days=60, retention_archive_days=0)
    res = retention.run(cfg, st, dry_run=True)
    assert [n for _, n in res['messages']] == [2]                                     # light + 没画像的 none 各 1 条
    assert len(_ids(st, 'light')) == 3                                                 # dry run 不删
    retention.run(cfg, st)
    assert _ids(st, 'light') == ['light-1', 'light-40'] and _ids(st, 'none') == ['none-1', 'none-40']
    assert len(_ids(st, 'mine')) == 3                                                  # 只存不看关掉了
    assert retention.plan(dict(DEFAULTS, retention_archive_days=0), Storage(st.url)) == []


def test_housekeep_and_vacuum(st, monkeypatch):
    old = datetime.now() - timedelta(days=200)
    s = st.Session()
    s.add_all([InboxItem(status='done', created_at=old), InboxItem(status='pending', created_at=old),
               Task(kind='task', status='done', created_at=old), Task(kind='task', status='pending', created_at=old),
               Task(kind='task', status='done', every_sec=60, created_at=old),
               Task(kind='event', status='done', created_at=old),
               Control(status='done', created_at=old), JobRun(job='x', status='ok', created_at=old),
               JobRun(job='x', status='running', created_at=old), InboxItem(status='done')])
    s.commit()
    s.close()
    vac = []
    monkeypatch.setattr(st, 'vacuum', lambda: vac.append(1))
    state = {}
    res = retention.run(dict(DEFAULTS), st, state=state)
    assert dict(res['records']) == {'处理完的收件箱': 1, '办完的一次性任务': 1, '处理完的控制请求': 1, '例行任务运行记录': 1}
    assert vac and state['last_vacuum']
    retention.run(dict(DEFAULTS), st, state=state)                                     # 没删东西不整理
    assert len(vac) == 1
    assert '旧记录: 删 处理完的收件箱 1' in retention.summary(res)


def _prune_args(**kw):
    return argparse.Namespace(**dict(dict(days=None, chat=[], dry_run=False, json=False), **kw))


def test_prune_cli(st, monkeypatch, capsys):
    monkeypatch.setattr(cli, 'Storage', lambda: st)
    monkeypatch.setattr(cli, 'load_config', lambda: A30)
    _setup(st)
    assert cli.cmd_prune(_prune_args(chat=['项目群'])) == 1                            # --chat 要配 --days
    assert cli.cmd_prune(_prune_args(days=30)) == 1                                    # --days 要配 --chat
    assert cli.cmd_prune(_prune_args(days=30, chat=['项目群'], dry_run=True)) == 0
    assert '会删 2 条' in capsys.readouterr().out
    assert cli.cmd_prune(_prune_args(days=30, chat=['项目群'])) == 0
    assert _ids(st, 'focus') == ['focus-1']
    assert cli.cmd_prune(_prune_args(dry_run=True)) == 0
    assert '只存不看 2 个会话, 保留 30 天: 会删 4 条' in capsys.readouterr().out


def test_agent_cannot_prune(monkeypatch):
    from larkx.agent.hooks import check_tool_use
    assert check_tool_use('Bash', {'command': 'larkx prune --dry-run'})
    monkeypatch.setenv('LARKX_ACTOR', 'agent')
    monkeypatch.setattr('sys.argv', ['larkx', 'prune'])
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert e.value.code == 3
