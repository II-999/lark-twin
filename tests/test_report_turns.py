"""给本人的汇报(report 轮次)、维护任务里安排提醒、配置里隐藏密码。"""
import argparse
import asyncio
import json
import time

from larkx import cli, state
from larkx.storage import Storage


def turn(kind='message', chat='c1', owner=False):
    return {'kind': kind, 'chat_id': chat, 'anchor': '', 'is_owner': owner, 'ref': 'r1'}


def test_report_only_sends_to_owner_chat():
    assert state.send_denied('self', turn('report', chat='self')) == ''
    assert '本人私聊' in state.send_denied('c2', turn('report', chat='self'))
    assert state.send_denied('self', turn('system', chat='self'))          # 维护任务照旧不能发消息
    assert not state.is_guest(turn('report', chat='self'))


def test_report_prompt_names_owner_chat():
    from larkx.daemon.triggers import build_report_prompt
    p = build_report_prompt('briefing', '今天<没事>', 'self')
    assert p.startswith('<report kind="briefing">今天&lt;没事&gt;</report>')
    assert "larkx send self -m - <<'EOF'" in p


def test_takeover_summary_is_report(tmp_path, monkeypatch, fake_client):
    from tests.test_reliability import _daemon
    obj = _daemon(tmp_path, fake_client, monkeypatch)
    obj.storage.takeover_start('p1', time.time() - 1, goal='约时间')
    asyncio.run(obj._finish_takeovers())
    ev = obj.inbox.get_nowait()
    assert (ev.kind, ev.chat_id, ev.is_owner) == ('report', obj.notifier.owner_chat(), False)
    assert '<report kind="takeover">' in ev.prompt and '<system>' not in ev.prompt and '<task' not in ev.prompt


def _task_args(text, **kw):
    base = dict(action='add', text=text, at=None, in_='10m', every=None, chat=None, root=None, all=False, json=True)
    base.update(kw)
    return argparse.Namespace(**base)


def test_system_turn_can_schedule_reminder_in_owner_chat(tmp_path, monkeypatch, capsys):
    st = Storage(f'sqlite:///{tmp_path}/t.db')
    monkeypatch.setattr(cli, 'Storage', lambda: st)
    monkeypatch.setenv('LARKX_ACTOR', 'agent')
    monkeypatch.setattr(state, 'read_turn', lambda: turn('system', chat='self'))
    assert cli.cmd_task(_task_args(['提醒', '回复张三'])) == 0
    t = st.list_tasks()[0]
    assert (t.chat_id, t.by_owner, t.creator) == ('self', 0, 'system')
    assert cli.cmd_task(_task_args(['发到别处'], chat='c2')) == 1          # 只能落在本人私聊
    monkeypatch.setattr(state, 'read_turn', lambda: turn('system', chat=''))
    assert cli.cmd_task(_task_args(['没有本人私聊'])) == 1
    capsys.readouterr()


def test_config_hides_password(monkeypatch, capsys):
    monkeypatch.setenv('LARKX_STORAGE_URL', 'mysql+pymysql://u:secret@db:3306/larkx')
    assert cli.cmd_config(argparse.Namespace()) == 0
    out = json.loads(capsys.readouterr().out)
    assert 'secret' not in out['storage_url'] and out['storage_url'].startswith('mysql+pymysql://u:***@db')
