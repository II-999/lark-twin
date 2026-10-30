"""larkx forward: 协议层的转发请求、daemon 不把转发出去的消息当成本人的指令、CLI 的管控和 send 一致。"""
import argparse
import asyncio
import json
import sys
import time

import pytest

from larkx import cli, guest, state
from larkx.protocol.client import LarkClient
from larkx.protocol.region import get_region
from larkx.storage import Storage

OWNER = '100'
SELF, G1, G2, C1, C2, TARGET = '699', '501', '502', '601', '602', '603'  # 本人私聊、来源会话、目标会话


# ---- 协议层 ----

def _client(resp=None):
    """resp 是转发接口的返回; 记下每次调用的接口和参数。"""
    c = object.__new__(LarkClient)
    c.auth = type('A', (), {'user_id': OWNER, 'region': get_region('feishu')})()
    calls = []

    def api(cmd, data):
        calls.append((cmd, data))
        return resp(cmd, data) if callable(resp) else resp

    c.api = api
    return c, calls


def _msg(mid, chat, **kw):
    return {'id': mid, 'chatId': chat, 'fromId': OWNER, 'type': 4, 'position': 7, **kw}


def test_forward_message_registers_each_copy():
    c, calls = _client(lambda cmd, data: {'messages': {'c1': _msg('11', 'c1', cid=data['cids']['c1']), 'c2': {'id': '-1'}}})
    got = c.forward_message('m1', ['c1', 'c2', 'c3'])
    # 一次请求发往全部目标; 每个目标一个 cid, 发出前登记好, 只认它所在的那个会话
    (cmd, req), = calls
    assert cmd == 'messages.PutTransmitMessageRequest'
    assert (req['chatIds'], req['messageId'], sorted(req['cids'])) == (['c1', 'c2', 'c3'], 'm1', ['c1', 'c2', 'c3'])
    assert len(set(req['cids'].values())) == 3
    for chat, cid in req['cids'].items():
        assert state.is_forward({'cid': cid, 'chat_id': chat})
        assert not state.is_forward({'cid': cid, 'chat_id': 'other'})
    # 拒收(-1)、响应里没有的会话不算发出
    assert list(got) == ['c1'] and (got['c1']['msg_id'], got['c1']['chat_id'], got['c1']['position']) == ('11', 'c1', 7)


def test_forward_without_mark_is_not_registered():
    c, calls = _client(lambda cmd, data: {'messages': {'c1': _msg('11', 'c1', cid=data['cids']['c1'])}})
    assert c.forward_message('m1', ['c1'], mark=False)['c1']['msg_id'] == '11'
    assert not state.is_forward({'cid': calls[0][1]['cids']['c1'], 'chat_id': 'c1'})


def test_relay_copy_is_not_registered_as_forward(monkeypatch):
    """匿名发送经本人私聊中转过去的那份是本人或数字人要发的消息, 照常交给 daemon 判断, 不登记成转发。"""
    from larkx.protocol import client as client_mod
    c, calls = _client(lambda cmd, data: {'messages': {'c1': _msg('fwd', 'c1')}} if 'Transmit' in cmd else {})
    c._self_chat, c._gateway_post = 'self', lambda pkt: b''
    monkeypatch.setattr(client_mod.builders, 'decode_put_message_response', lambda content: 'r1')
    assert c._send_via_relay({'kind': 'text', 'text': 'hi'}, 'c1') == 'fwd'
    assert [cmd for cmd, _ in calls] == ['messages.PutTransmitMessageRequest', 'messages.DeleteMessageRequest']
    assert not state.FORWARD_DIR.exists() or not any(state.FORWARD_DIR.iterdir())


def test_merge_forward_request():
    c, calls = _client(lambda cmd, data: {'messages': {'c1': _msg('21', 'c1', type=11)}, 'failChatIds': {'c2': 'x'}})
    got = c.merge_forward(['m1', 'm2'], ['c1', 'c2'])
    (cmd, req), = calls
    assert cmd == 'messages.PutMergeForwardMessageRequest'
    assert (req['messageIds'], req['toChatIds'], req['type'], req['locale']) == (['m1', 'm2'], ['c1', 'c2'], 'MESSAGE', 'zh_CN')
    assert all(state.is_forward({'cid': cid, 'chat_id': chat}) for chat, cid in req['cids'].items())
    assert list(got) == ['c1'] and got['c1']['msg_type_name'] == 'MERGE_FORWARD'


def test_forward_to_thread_chat_reads_thread_messages():
    resp = {'threadMessages': {'x': {'chatId': 't1', 'message': _msg('31', 't1')},
                               'y': {'chatId': 't2', 'message': _msg('32', 't2'), 'failed': True}}}
    c, _ = _client(resp)
    assert {k: v['msg_id'] for k, v in c.forward_message('m1', ['t1', 't2']).items()} == {'t1': '31'}


def test_get_messages_is_batched_and_keeps_only_asked_ids():
    def resp(cmd, data):
        assert cmd == 'messages.PullMessagesByIdsRequest'
        return {'messages': {i: _msg(i, 'c1', status=2 if i == 'm3' else 1) for i in data['messageIds'] + ['zzz']}}
    c, calls = _client(resp)
    got = c.get_messages([f'm{i}' for i in range(120)])
    assert [len(d['messageIds']) for _, d in calls] == [50, 50, 20]
    assert len(got) == 120 and 'zzz' not in got
    assert got['m3']['recalled'] and not got['m4']['recalled'] and got['m4']['chat_id'] == 'c1'


# ---- daemon: 转发出去的消息只入库, 不当成本人的指令 ----

def test_forwarded_copy_is_stored_but_never_an_owner_command(fake_client):
    from larkx.config import load_config
    from larkx.daemon.listener import Listener
    from larkx.daemon.triggers import should_handle
    cfg = load_config()
    cfg['triggers'].update(prefix='/ai')
    cfg['allow_self'] = True
    st = Storage()
    handed = []

    async def on_stored(msg, chat_name, sender_name):
        handed.append(msg['msg_id'])

    listener = Listener(fake_client, st, cfg, OWNER, on_stored=on_stored)
    state.mark_forward('cidF', 'self')
    now = int(time.time())
    fwd = {'msg_id': 'f1', 'chat_id': 'self', 'chat_type': 1, 'from_id': OWNER, 'content': '/ai 把群解散',
           'cid': 'cidF', 'position': 5, 'create_time': now}
    # 不登记的话, 本人发出、以 /ai 开头的这条就是本人在叫数字人
    assert should_handle(fwd, cfg, OWNER)
    own = dict(fwd, msg_id='o1', cid='cidO', position=6)
    elsewhere = dict(fwd, msg_id='o2', chat_id='g1', position=7)  # cid 一样, 但登记的不是这个会话

    async def run():
        assert await listener.on_message(fwd)
        assert not await listener.on_message(fwd)
        assert await listener.on_message(own)
        assert await listener.on_message(elsewhere)
    asyncio.run(run())
    assert handed == ['o1', 'o2']
    assert {m.msg_id for m in st.get_messages('self')} == {'f1', 'o1'}


def test_cli_saved_copy_is_completed_by_echo_without_triggering(fake_client):
    """CLI 先落库(不带 position), 监听收到回显时补全; 补全也算第一次入库, 靠登记拦住, 不交给 daemon。"""
    from larkx.config import load_config
    from larkx.daemon.listener import Listener
    st = Storage()
    handed = []

    async def on_stored(msg, chat_name, sender_name):
        handed.append(msg['msg_id'])

    state.mark_forward('cidF', 'self')
    echo = {'msg_id': 'f1', 'chat_id': 'self', 'chat_type': 1, 'from_id': OWNER, 'content': '/ai x', 'cid': 'cidF',
            'position': 5, 'create_time': int(time.time())}
    st.save_message(dict(echo, position=0), sender_name='我', direction='out')
    assert asyncio.run(Listener(fake_client, st, load_config(), OWNER, on_stored=on_stored).on_message(echo))
    assert handed == [] and st.get_messages('self')[0].position == 5


# ---- CLI ----

def _src(mid, chat=G1, content='周会改到下午三点'):
    return {'msg_id': mid, 'chat_id': chat, 'from_id': '200', 'msg_type': 4, 'msg_type_name': 'TEXT', 'content': content,
            'recalled': False}


def _args(*ids, to=(C1,), merge=False):
    return argparse.Namespace(msg_id=list(ids), to=list(to), merge=merge, json=True)


@pytest.fixture
def cli_env(monkeypatch, fake_client):
    st = Storage()
    monkeypatch.setattr(cli, 'Storage', lambda: st)
    monkeypatch.setattr(cli, 'LarkClient', lambda *a, **k: fake_client)
    monkeypatch.setattr(cli, 'LarkAuth', lambda *a, **k: None)
    monkeypatch.delenv('LARKX_ACTOR', raising=False)
    monkeypatch.setenv('LARKX_TRIGGER_PREFIX', '/ai')
    fake_client._owner_chat = SELF
    fake_client.messages = {m: _src(m) for m in ('1', '2', '3')}
    return st, fake_client


def _out(capsys):
    return json.loads(capsys.readouterr().out)


def test_human_forwards_one_by_one(cli_env, capsys):
    st, fc = cli_env
    assert cli.cmd_forward(_args('2', '1', '2', to=(C1, C2, C1))) == 0
    # 去重后按给的顺序, 每条一次请求, 发往全部目标
    assert fc.forwards == [('forward', ['2'], [C1, C2]), ('forward', ['1'], [C1, C2])]
    out = _out(capsys)
    assert out['ok'] and not out['merge'] and out['failed'] == []
    assert [(m['chat_id'], m['msg_id'], m['source_ids']) for m in out['messages']] == [
        (C1, f'f1-{C1}', ['2']), (C2, f'f1-{C2}', ['2']), (C1, f'f2-{C1}', ['1']), (C2, f'f2-{C2}', ['1'])]
    rows = st.list_actions()
    assert [(a.actor, a.action, a.chat_id, a.msg_id, a.ok) for a in rows] == [
        ('human', 'forward', C1, f'f1-{C1}', 1), ('human', 'forward', C2, f'f1-{C2}', 1),
        ('human', 'forward', C1, f'f2-{C1}', 1), ('human', 'forward', C2, f'f2-{C2}', 1)]
    assert rows[0].content == '[转发 2] 周会改到下午三点'
    # 先落库, 不带 position, 等监听收到回显时补全
    saved = st.get_messages(C1)
    assert sorted((m.msg_id, m.direction, m.position, m.sender_name) for m in saved) == [
        (f'f1-{C1}', 'out', 0, '我'), (f'f2-{C1}', 'out', 0, '我')]


def test_human_merge_forward(cli_env, capsys):
    st, fc = cli_env
    assert cli.cmd_forward(_args('1', '2', to=(C1, C2), merge=True)) == 0
    assert fc.forwards == [('merge', ['1', '2'], [C1, C2])]
    out = _out(capsys)
    assert out['merge'] and [(m['chat_id'], m['source_ids']) for m in out['messages']] == [(C1, ['1', '2']), (C2, ['1', '2'])]
    rows = st.list_actions()
    assert [(a.chat_id, a.msg_id) for a in rows] == [(C1, f'f1-{C1}'), (C2, f'f1-{C2}')]
    assert rows[0].content.startswith('[合并转发 2 条: 1 2]')


def test_text_output(cli_env, capsys):
    args = _args('1')
    args.json = False
    assert cli.cmd_forward(args) == 0
    assert capsys.readouterr().out == f'已转发 1 → {C1}: msg_id=f1-{C1}\n'
    args.merge, args.msg_id = True, ['1', '2']
    assert cli.cmd_forward(args) == 0
    assert capsys.readouterr().out == f'已合并转发 2 条 → {C1}: msg_id=f2-{C1}\n'


def test_merge_needs_one_source_chat(cli_env, capsys):
    st, fc = cli_env
    fc.messages['3'] = _src('3', chat=G2)
    assert cli.cmd_forward(_args('1', '3', merge=True)) == 1
    assert '同一个会话' in _out(capsys)['error'] and fc.forwards == [] and st.list_actions() == []


@pytest.mark.parametrize('ids,to,why', [
    (('om_x100b617b',), (C1,), '都是数字'),
    (('1',), ('oc_d3c7',), '都是数字'),
    (('1', '9'), (C1,), '找不到这些消息'),
    (('4',), (C1,), '找不到这些消息'),
])
def test_bad_or_missing_ids(cli_env, capsys, ids, to, why):
    st, fc = cli_env
    fc.messages['4'] = dict(_src('4'), recalled=True)
    assert cli.cmd_forward(_args(*ids, to=to)) == 1
    assert why in _out(capsys)['error'] and fc.forwards == [] and st.list_actions() == []


def test_too_many_at_once(cli_env, capsys):
    _, fc = cli_env
    assert cli.cmd_forward(_args(*[str(i) for i in range(cli.FORWARD_MAX + 1)])) == 1
    assert '最多' in _out(capsys)['error'] and fc.forwards == []


def test_partial_failure_is_reported_and_audited(cli_env, capsys):
    st, fc = cli_env
    fc.forward_error['2'] = RuntimeError('boom')
    assert cli.cmd_forward(_args('1', '2', '3')) == 1
    out = _out(capsys)
    assert not out['ok'] and [m['source_ids'] for m in out['messages']] == [['1'], ['3']]
    assert out['failed'] == [{'chat_id': C1, 'source_ids': ['2'], 'error': '转发失败: boom'}]
    assert [(a.msg_id, a.ok, a.error) for a in st.list_actions()] == [
        (f'f1-{C1}', 1, ''), ('', 0, '转发失败: boom'), (f'f3-{C1}', 1, '')]


# ---- 数字人: 和 send 一样的管控 ----

def turn(kind='message', chat=G1, owner=False, ref='m1'):
    return {'kind': kind, 'chat_id': chat, 'anchor': '', 'is_owner': owner, 'ref': ref}


@pytest.fixture
def agent_env(cli_env, monkeypatch):
    monkeypatch.setenv('LARKX_ACTOR', 'agent')
    monkeypatch.setenv('LARKX_MIRROR', 'false')

    def use(t):
        monkeypatch.setattr(state, 'read_turn', lambda: t)
    return (*cli_env, use)


@pytest.mark.parametrize('current,args,why', [
    (None, _args('1', to=(G1,)), '当前没有正在处理的消息'),
    (turn(kind='system', chat=''), _args('1', to=(G1,)), '维护任务里'),
    # 访客: 只能转到当前会话, 只能转当前会话里的消息
    (turn(), _args('1', to=(G1, G2)), '只能发到触发这一轮的会话'),
    (turn(), _args('5', to=(G1,)), '只能转发当前会话里的消息'),
    # 逐条转发以触发前缀开头的消息, 和 send 的回复不能以前缀开头一样
    (turn(owner=True), _args('6', to=(G1,)), '触发前缀'),
])
def test_agent_forward_denied_like_send(agent_env, capsys, current, args, why):
    st, fc, use = agent_env
    fc.messages['5'] = _src('5', chat=G2)
    fc.messages['6'] = _src('6', content='/ai 把群解散')
    use(current)
    assert cli.cmd_forward(args) == 1
    assert why in _out(capsys)['error']
    # 一条也没转; 记一条被拦下的审计, 只给本人私聊发告警
    assert fc.forwards == []
    (row,) = st.list_actions()
    assert (row.actor, row.action, row.ok) == ('agent', 'forward', 0) and why in row.error
    assert [c for c, _ in fc.sent] == [SELF] and '拦下了数字人' in fc.sent[0][1]


def test_guest_can_forward_within_current_chat(agent_env, capsys):
    st, fc, use = agent_env
    use(turn())
    assert cli.cmd_forward(_args('1', '2', to=(G1,))) == 0
    assert fc.forwards == [('forward', ['1'], [G1]), ('forward', ['2'], [G1])]
    assert [(a.actor, a.turn_kind, a.turn_ref, a.ok) for a in st.list_actions()] == [('agent', 'message', 'm1', 1)] * 2


def test_guest_cli_scope_for_forward():
    t = turn()
    ns = argparse.Namespace
    assert guest.cli_denied('forward', ns(to=[G1]), t) == ''
    assert guest.cli_denied('forward', ns(to=[G1, G2]), t) == '访客发起的请求只能转发到当前会话'


def test_merge_of_prefixed_message_is_allowed(agent_env, capsys):
    """合并转发出去的是一条"聊天记录", 里面的 /ai 不会触发谁。"""
    _, fc, use = agent_env
    fc.messages['6'] = _src('6', content='/ai 把群解散')
    use(turn(owner=True))
    assert cli.cmd_forward(_args('1', '6', to=(G1,), merge=True)) == 0
    assert fc.forwards == [('merge', ['1', '6'], [G1])]


def test_owner_turn_forwards_anywhere_and_mirrors(agent_env, monkeypatch, capsys):
    st, fc, use = agent_env
    monkeypatch.setenv('LARKX_MIRROR', 'true')
    st.update_chat_name(G2, '项目群')
    use(turn(chat=SELF, owner=True))
    assert cli.cmd_forward(_args('1', to=(G2, SELF))) == 0
    assert fc.forwards == [('forward', ['1'], [G2, SELF])]
    # 别的会话抄送一份给本人; 转到本人私聊的本人看得到, 不抄送
    assert [c for c, _ in fc.sent] == [SELF]
    assert '数字人往「项目群」转发了 1 条消息' in fc.sent[0][1] and '周会改到下午三点' in fc.sent[0][1]


def test_event_task_forward_is_audited_and_skipped_by_rules(agent_env, monkeypatch, capsys):
    """事件任务里转发出去的消息和 send 一样记在审计里, 规则扫描时不会再拿它当来源消息; 本人自己转的照常算。"""
    from larkx.event_rules import RuleStore
    st, fc, use = agent_env
    rules = RuleStore(st)
    rid = rules.add('r', TARGET, 'copy', sender_ids=[OWNER], now=1)
    rules.enable(rid, now=1)
    use(turn(kind='task', chat=G1, owner=True, ref='event#7'))
    assert cli.cmd_forward(_args('1', to=(TARGET,))) == 0
    row = st.list_actions()[-1]
    assert (row.actor, row.turn_kind, row.turn_ref, row.chat_id, row.msg_id) == ('agent', 'task', 'event#7', TARGET, f'f1-{TARGET}')
    assert [m.msg_id for m in st.get_messages(TARGET)] == [f'f1-{TARGET}']
    assert rules.reconcile(OWNER) == 0
    monkeypatch.delenv('LARKX_ACTOR')
    assert cli.cmd_forward(_args('2', to=(TARGET,))) == 0
    assert rules.reconcile(OWNER) == 1


def test_rate_limit_counts_sends_and_forwards(agent_env, monkeypatch, capsys):
    st, fc, use = agent_env
    monkeypatch.setenv('LARKX_SEND_RATE_LIMIT', '3')
    use(turn(owner=True))
    send = lambda: cli.cmd_send(argparse.Namespace(text=['好'], json=True, chat_id=G1, root=None))
    assert send() == 0 and send() == 0
    # 已经发了 2 条, 再逐条转发 2 条就超过 3 条的上限; 转 1 条正好
    assert cli.cmd_forward(_args('1', '2', to=(G1,))) == 1
    assert '上限' in st.list_actions()[-1].error
    assert cli.cmd_forward(_args('1', to=(G1,))) == 0
    # 转发也算进 send 的上限
    assert send() == 1 and '上限' in st.list_actions()[-1].error


def test_agent_can_recall_own_forward(agent_env, capsys):
    st, fc, use = agent_env
    use(turn())
    st.log_action(actor='agent', action='forward', chat_id=G1, msg_id='71', content='x')
    st.log_action(actor='human', action='forward', chat_id=G1, msg_id='72', content='x')
    assert cli.cmd_recall(argparse.Namespace(msg_id='72', json=True)) == 1
    assert cli.cmd_recall(argparse.Namespace(msg_id='71', json=True)) == 0
    assert fc.api_calls == [('messages.DeleteMessageRequest', {'id': '71'})]
    assert st.list_actions()[-1].chat_id == G1


def test_main_parses_forward(monkeypatch):
    got = []
    monkeypatch.setattr(cli, 'cmd_forward', lambda args: got.append(args) or 0)
    monkeypatch.setattr(sys, 'argv', ['larkx', 'forward', '1', '2', '--to', C1, '--to', C2, '--merge', '--json'])
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert e.value.code == 0
    assert (got[0].msg_id, got[0].to, got[0].merge, got[0].json) == (['1', '2'], [C1, C2], True, True)
    monkeypatch.setattr(sys, 'argv', ['larkx', 'forward', '1'])
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert e.value.code == 2  # 必须给 --to

