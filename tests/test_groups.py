"""会话画像、分类与关注度: 建议规则、本人确认优先、按名字管理、回顾提示、CLI 权限。"""
import argparse
import json
import time
from datetime import datetime

import pytest

from larkx import cli, groups, state
from larkx.storage import Storage
from larkx.storage.models import ChatProfile

OWNER = '100'


def prof(**kw):
    base = dict(chat_id='c', name='', chat_type=2, members=10, peer_id='', peer_bot=-1, feishu_muted=-1,
                feishu_pinned=0, msgs=5, owner_msgs=0, at_me=0)
    base.update(kw)
    return ChatProfile(**base)


@pytest.mark.parametrize('kw, expect', [
    (dict(chat_type=1, peer_id=OWNER), ('本人', 'archive')),
    (dict(chat_type=1, peer_id='9', peer_bot=1, name='审批'), ('需要你处理的通知', 'light')),
    (dict(chat_type=1, peer_id='9', peer_bot=1, name='字节餐厅'), ('行政生活', 'archive')),
    (dict(chat_type=1, peer_id='9', peer_bot=1, name='Argos平台报警'), ('机器人推送', 'archive')),
    (dict(chat_type=1, peer_id='9', peer_bot=0, owner_msgs=5), ('同事私聊', 'focus')),
    (dict(chat_type=1, peer_id='9', peer_bot=0, owner_msgs=1), ('同事私聊', 'light')),
    (dict(name='TaeBuddy4测试群'), ('测试群', 'archive')),
    (dict(name='项目A 测试', owner_msgs=4), ('工作群', 'focus')),          # 自己常发言的不算测试群
    (dict(name='项目群', at_me=2), ('工作群', 'focus')),
    (dict(name='项目A试用', feishu_pinned=1), ('工作群', 'light')),        # 置顶至少简单关注
    (dict(name='OceanAI 稳定性报警值班群'), ('告警', 'light')),
    (dict(name='底表 TTL 调整周知', members=488), ('部门周知', 'light')),
    (dict(name='Claude Code闲聊群', members=3500), ('大群', 'archive')),
    (dict(name='老项目群', msgs=0), ('沉寂', 'archive')),
    (dict(name='一个群', feishu_muted=1), ('工作群', 'light')),                 # 免打扰不降级
])
def test_suggest_rules(kw, expect):
    assert groups.suggest(prof(**kw), OWNER)[:2] == expect


@pytest.fixture
def st(tmp_path):
    return Storage(f'sqlite:///{tmp_path}/g.db')


def _msg(st, mid, chat, sender, content='hi', chat_type=2, name='', age=3600, data=None):
    st.save_message({'msg_id': mid, 'chat_id': chat, 'chat_type': chat_type, 'from_id': sender, 'content': content,
                     'create_time': int(time.time() - age), 'content_data': data or {}},
                    sender_name='我' if sender == OWNER else '张三', chat_name=name)


def test_attention_precedence_and_confirmed_kept(st):
    cs = groups.ChatStore(st)
    _msg(st, 'm1', 'g1', '200', name='项目群')
    cs.save_profile('g1', OWNER, name='项目群', chat_type=2, msgs=1)
    assert cs.view(['g1'])[0]['attention_source'] == '建议'
    g = cs.create_group('项目A', 'focus', ['发布'])
    cs.assign(['g1'], group_id=g.id)
    row = cs.view(['g1'])[0]
    assert (row['group'], row['attention'], row['attention_source'], row['keywords']) == \
        ('项目A', 'focus', '跟分类', ['发布'])
    cs.assign(['g1'], attention='archive')
    assert cs.view(['g1'])[0]['attention_source'] == '单独设置'
    cs.save_profile('g1', OWNER, owner_msgs=9)                       # 画像只改信号和建议, 不动本人设置的分类
    row = cs.view(['g1'])[0]
    assert (row['group'], row['attention'], row['owner_msgs']) == ('项目A', 'archive', 9)
    assert cs.delete_group(g.id) == 1
    assert cs.view(['g1'])[0]['group'] == '' and cs.profile('g1').confirmed


def test_profile_all_uses_server_signals(st, fake_client):
    _msg(st, 'm1', 'g1', OWNER, name='项目群')
    _msg(st, 'm2', 'g1', '200', content='@我 看下', data={'_larkx_mention_ids': [OWNER]})
    _msg(st, 'm3', 'g1', OWNER)
    _msg(st, 'm4', 'g1', OWNER)
    _msg(st, 'm5', 'p1', '300', chat_type=1, name='审批')
    fake_client.auth.user_id = OWNER
    fake_client.bots = {'300'}
    fake_client.api_responses = {
        'feed.PullShortcutsRequest': {'shortcuts': [{'id': 'g1', 'type': 1}]},
        'chats.PullChatsByIdsRequest': lambda d: {'chats': {
            'g1': {'type': 2, 'name': '项目群', 'memberCount': 12, 'userSetting': {'isRemind': False}},
            'p1': {'type': 1, 'key': f'{OWNER}:300', 'memberCount': 2, 'userSetting': {'isRemind': True}}}},
    }
    res = groups.profile_all(fake_client, st, OWNER)
    assert res['chats'] == 2 and sorted(res['new']) == ['g1', 'p1']
    v = {r['chat_id']: r for r in groups.ChatStore(st).view()}
    assert (v['g1']['owner_msgs'], v['g1']['at_me'], v['g1']['feishu_pinned'], v['g1']['feishu_muted']) == (3, 1, True, True)
    assert v['g1']['attention'] == 'focus' and v['g1']['members'] == 12
    assert (v['p1']['peer_bot'], v['p1']['suggested_group']) == (True, '需要你处理的通知')
    fake_client.bots = set()
    groups.profile_all(fake_client, st, OWNER)                        # 机器人只查一次, 记住了
    assert groups.ChatStore(st).view(['p1'])[0]['peer_bot']


def test_review_prompt_lists_by_attention(st):
    from larkx.daemon import prompts
    cs = groups.ChatStore(st)
    _msg(st, 'm1', 'g1', '200', name='项目群')
    _msg(st, 'm2', 'g2', '200', name='大群')
    _msg(st, 'm3', 'g3', '200', name='校招群')
    cs.save_profile('g1', OWNER, name='项目群', chat_type=2, msgs=1, owner_msgs=5, feishu_muted=1)
    cs.save_profile('g2', OWNER, name='大群', chat_type=2, members=999, msgs=1)
    priv = cs.create_group('私人', 'light', private=True)
    cs.assign(['g3'], group_id=priv.id)
    text = prompts.review({'chat_groups': True}, st, datetime.now(), OWNER)
    assert '[重点关注/免打扰] 项目群 (g1)' in text and '[简单关注/私人] 校招群 (g3)' in text
    assert '大群 (g2)' not in text and '另有 1 个只存不看的会话' in text


def _args(**kw):
    base = dict(action='list', targets=[], to=None, attention=None, keywords=None, name=None, private=None, json=False)
    base.update(kw)
    return argparse.Namespace(**base)


def test_groups_cli(st, monkeypatch, capsys):
    monkeypatch.setattr(cli, 'Storage', lambda: st)
    _msg(st, 'm1', 'g1', '200', name='项目A研发支持小分队')
    _msg(st, 'm2', 'g2', '200', name='项目A试用')
    assert cli.cmd_groups(_args(action='add', targets=['项目A'], attention='重点', keywords='发布,回归')) == 0
    assert cli.cmd_groups(_args(action='assign', targets=['项目A'], to='项目A')) == 1   # 名字对上两个会话
    assert cli.cmd_groups(_args(action='assign', targets=['项目A试用', 'g1'], to='项目A')) == 0
    assert cli.cmd_groups(_args(action='assign', targets=['项目A试用'], attention='简单关注')) == 0
    capsys.readouterr()
    assert cli.cmd_groups(_args(json=True)) == 0
    out = json.loads(capsys.readouterr().out)
    assert out['groups'][0]['chats'] == 2 and out['groups'][0]['keywords'] == ['发布', '回归']
    rows = {r['chat_id']: r for r in groups.ChatStore(st).view()}
    assert rows['g2']['attention'] == 'light' and rows['g1']['attention'] == 'focus'
    assert any(a.action == 'group' for a in st.list_actions())
    assert cli.cmd_groups(_args(action='edit', targets=['项目A'], attention='只存', name='A组')) == 0
    assert groups.ChatStore(st).view(['g1'])[0]['attention'] == 'archive'
    assert cli.cmd_groups(_args(action='remove', targets=['A组'])) == 0


def test_groups_cli_owner_only_for_agent(st, monkeypatch, capsys):
    monkeypatch.setattr(cli, 'Storage', lambda: st)
    monkeypatch.setenv('LARKX_ACTOR', 'agent')
    monkeypatch.setattr(state, 'read_turn', lambda: {'kind': 'system', 'chat_id': 'self', 'is_owner': False})
    assert cli.cmd_groups(_args(action='add', targets=['X'])) == 3
    assert cli.cmd_groups(_args()) == 0                                # 看可以
    monkeypatch.setattr(state, 'read_turn', lambda: {'kind': 'message', 'chat_id': 'self', 'is_owner': True})
    assert cli.cmd_groups(_args(action='add', targets=['X'])) == 0
    capsys.readouterr()


def test_chats_and_messages_filters(st, monkeypatch, capsys):
    monkeypatch.setattr(cli, 'Storage', lambda: st)
    _msg(st, 'm1', 'g1', '200', name='项目群', age=60)
    _msg(st, 'm2', 'g1', '200', name='项目群', age=3 * 86400)
    _msg(st, 'm3', 'g2', '200', name='大群', age=60)
    cs = groups.ChatStore(st)
    cs.save_profile('g1', OWNER, name='项目群', chat_type=2, msgs=2, owner_msgs=5, feishu_muted=1, last_at=int(time.time()))
    cs.save_profile('g2', OWNER, name='大群', chat_type=2, members=999, msgs=1, last_at=int(time.time()))
    from larkx.config import DEFAULTS
    monkeypatch.setattr(cli, 'load_config', lambda: dict(DEFAULTS, chat_groups=True))
    ns = dict(attention=None, group=None, active=None, feishu_muted=False, limit=50, json=True)
    assert cli.cmd_chats(argparse.Namespace(**dict(ns, attention='重点关注'))) == 0
    assert [r['chat_id'] for r in json.loads(capsys.readouterr().out)] == ['g1']
    assert cli.cmd_chats(argparse.Namespace(**dict(ns, feishu_muted=True))) == 0
    assert [r['chat_id'] for r in json.loads(capsys.readouterr().out)] == ['g1']
    assert cli.cmd_chats(argparse.Namespace(**dict(ns, json=False))) == 0
    assert '[重点关注·建议] [免打扰]' in capsys.readouterr().out
    margs = argparse.Namespace(chat_id='g1', anchor=None, since='1d', limit=50, json=True)
    assert cli.cmd_messages(margs) == 0
    assert [r['msg_id'] for r in json.loads(capsys.readouterr().out)] == ['m1']


def test_groups_off_keeps_existing_behavior(st, monkeypatch, capsys):
    """不开会话分类: chats 输出和原来一样, 回顾提示不变, 画像和清理任务不跑。"""
    from larkx import jobs
    from larkx.config import DEFAULTS
    from larkx.daemon import prompts
    monkeypatch.setattr(cli, 'Storage', lambda: st)
    monkeypatch.setattr(cli, 'load_config', lambda: dict(DEFAULTS))
    _msg(st, 'm1', 'g1', '200', name='项目群')
    assert cli.cmd_chats(argparse.Namespace(attention=None, group=None, active=None, feishu_muted=False, limit=50,
                                            json=False)) == 0
    line = capsys.readouterr().out.strip()
    assert line.startswith('g1\t群聊\t项目群\t更新于 ') and line.count('\t') == 3
    assert cli.cmd_chats(argparse.Namespace(attention=None, group=None, active=None, feishu_muted=False, limit=50,
                                            json=True)) == 0
    row = json.loads(capsys.readouterr().out)[0]
    assert {'chat_id', 'name', 'chat_type', 'updated_at'} <= set(row) and len(row['updated_at']) > 16
    assert prompts.review(dict(DEFAULTS), st, datetime.now(), OWNER) == prompts.BASIC_REVIEW_PROMPT
    assert jobs.get('profile').when(dict(DEFAULTS)) is None and jobs.get('prune').when(dict(DEFAULTS)) is None
    assert jobs.get('profile').when(dict(DEFAULTS, chat_groups=True)) is not None
    assert jobs.get('prune').when(dict(DEFAULTS, retention_archive_days=30)) is None            # 要先开分类
    assert jobs.get('prune').when(dict(DEFAULTS, retention_archive_days=30, chat_groups=True)) is not None
def test_failed_bot_lookup_stays_unknown(st):
    class Client:
        _bots = {}
        def api(self, name, data=None):
            return {}
        def is_bot(self, uid):
            return False        # 查失败: 按真人返回, 但没记进 _bots
        def get_user_name(self, uid, cid):
            return '某机器人'
    _msg(st, 'm1', 'p9', '900', chat_type=1)
    groups.profile_all(Client(), st, OWNER)
    assert groups.ChatStore(st).profile('p9').peer_bot == -1


def test_profile_many_chats_keeps_old_profiles(st):
    """前面查过私聊机器人以后, 后面的会话照样拿得到旧画像(上次活跃时间、查过的机器人结果、名字)。"""
    class Client:
        def __init__(self, bots):
            self._bots, self.asked = bots, []
        def api(self, name, data=None):
            if name == 'feed.PullShortcutsRequest':
                return {'shortcuts': [{'id': 'g2', 'type': 1}], 'hasMore': False}
            return {}
        def is_bot(self, uid):
            self.asked.append(uid)
            return uid in self._bots
        def get_user_name(self, uid, cid):
            return ''
    for cid, sender, kind in (('p1', '901', 1), ('g1', '200', 2), ('p2', '902', 1), ('g2', '200', 2)):
        _msg(st, f'm-{cid}', cid, sender, chat_type=kind, name='' if kind == 1 else f'群{cid}')
    cs = groups.ChatStore(st)
    cs.save_profile('p2', OWNER, peer_id='902', peer_bot=1, chat_type=1, name='老机器人', last_at=123)
    cs.save_profile('g2', OWNER, chat_type=2, name='群g2', last_at=456)
    c = Client({'901'})
    groups.profile_all(c, st, OWNER)
    assert c.asked == ['901']                                            # 查过的 p2 不再查
    p2, g2 = cs.profile('p2'), cs.profile('g2')
    assert (p2.peer_bot, p2.name) == (1, '老机器人') and cs.profile('p1').peer_bot == 1
    assert g2.last_at > 0 and g2.feishu_pinned == 1
    groups.profile_all(type('NoCache', (Client,), {})(set()), st, OWNER)   # client 没有 _bots 也不报错


def test_pins_unknown_keeps_previous(st):
    class Client:
        def api(self, name, data=None):
            return {'shortcuts': [{'id': 'g1', 'type': 1}], 'hasMore': True}   # 怎么都拿不全
    assert groups.feishu_pins(Client()) is None


def test_bot_action_keywords_configurable(st):
    cs = groups.ChatStore(st, groups.action_words({'bot_action_keywords': '工单, 发布单'}))
    cs.save_profile('b1', OWNER, chat_type=1, peer_id='9', peer_bot=1, name='发布单助手')
    assert cs.profile('b1').suggested_attention == 'light'
    groups.ChatStore(st).save_profile('b2', OWNER, chat_type=1, peer_id='9', peer_bot=1, name='发布单助手')
    assert groups.ChatStore(st).profile('b2').suggested_attention == 'archive'


def test_at_me_ignores_forwarded_records(st):
    """合并转发的记录里带着本人 id(fromId), 不算 @ 本人。"""
    _msg(st, 'f1', 'g1', '200', name='项目群', data={'forward': [{'fromId': OWNER, 'text': '之前说的'}]})
    _msg(st, 'a1', 'g1', '200', name='项目群', data={'_larkx_mention_ids': [OWNER]})
    stats = groups.ChatStore(st).stats(OWNER, '', 0)
    assert stats['g1']['at_me'] == 1 and stats['g1']['msgs'] == 2
