"""整理方案: 提交校验、编号、按条执行、撤销还原(分组、飞书标签、置顶、标记、免打扰)、权限、私信本人。"""
import argparse
import asyncio
import json
import time

import pytest

from larkx import cli, groups, organize, state
from larkx.storage import Storage

OWNER = '100'


class FakeFeishu:
    """记住置顶、标记、免打扰、标签的飞书客户端。"""

    def __init__(self):
        self.pins, self.flags, self.remind, self.labels, self.next_id = ['p0', 'g9'], set(), {}, {}, 1000

    def api(self, name, d=None):
        d = d or {}
        if name == 'feed.PullShortcutsRequest':
            return {'shortcuts': [{'id': i, 'type': 1} for i in self.pins]}
        if name == 'feed.PutShortcutsRequest':
            self.pins += [s['id'] for s in d['shortcuts'] if s['id'] not in self.pins]
        elif name == 'feed.DeleteShortcutsRequest':
            gone = {s['id'] for s in d['shortcuts']}
            self.pins = [i for i in self.pins if i not in gone]
        elif name == 'feed.PatchShortcutsRequest':
            self.pins = [s['id'] for s in sorted(d['shortcuts'], key=lambda s: s['position'])]
        elif name == 'feed.PullFlagsRequest':
            return {'items': [{'itemId': int(i)} for i in self.flags]}
        elif name == 'feed.UpdateFlagsRequest':
            self.flags |= {str(i['itemId']) for i in d.get('items') or []}
            self.flags -= {str(i['itemId']) for i in d.get('deletedItems') or []}
        elif name == 'chats.PullChatsByIdsRequest':
            return {'chats': {c: {'userSetting': {'isRemind': self.remind.get(c, True)}} for c in d['chatIds']}}
        elif name == 'feed.BatchMuteFeedCardsRequest':
            for c in d['feedCards']:
                self.remind[c['id']] = d['unmute']
        elif name == 'feed.PullFeedGroupsRequest':
            return {'feedGroups': [{'id': int(i), 'name': n} for i, (n, _) in self.labels.items()]}
        elif name == 'feed.PullFeedGroupItemsRequest':
            (gid, _), = d['args'].items()
            return {'data': {str(gid): {'feedGroupItems': [{'feedCardId': int(c)} for c in self.labels[str(gid)][1]]}}}
        elif name == 'feed.CreateFeedGroupRequest':
            self.next_id += 1
            self.labels[str(self.next_id)] = (d['feedGroup']['name'], [str(i['feedCardId']) for i in d['feedGroupItems']])
            return {'feedGroup': {'id': self.next_id}}
        elif name == 'feed.UpdateFeedGroupsRequest':
            for g in d.get('deletedGroups') or []:
                self.labels.pop(str(g['id']), None)
        return {}


@pytest.fixture
def st(tmp_path):
    s = Storage(f'sqlite:///{tmp_path}/o.db')
    for cid, name in (('11', '项目群'), ('12', '测试群'), ('13', '大群'), ('14', '陈威')):
        s.save_message({'msg_id': f'm{cid}', 'chat_id': cid, 'chat_type': 2, 'from_id': '200', 'content': 'x',
                        'create_time': int(time.time())}, chat_name=name)
    return s


PLAN = {'summary': '按业务分组',
        'groups': [{'name': 'InfraBuddy', 'attention': '重点关注', 'keywords': ['防劣化'], 'chats': ['项目群', '14'],
                    'reason': '主业务'},
                   {'name': '测试', 'attention': '只存不看', 'chats': ['测试群']}],
        'pin': [{'chat': '项目群', 'reason': '常用'}], 'unpin': [{'chat': 'g9', 'reason': '沉寂'}],
        'pin_order': ['p0', '项目群'],
        'flag': [{'chat': '陈威', 'msg_id': '777', 'text': '等你上线', 'reason': '待跟进'}],
        'mute': ['大群'], 'mute_reason': '大群没用'}


def test_submit_validates_and_numbers(st):
    store = organize.OrganizeStore(st)
    with pytest.raises(ValueError):
        store.submit({'groups': [{'name': 'X', 'chats': ['不存在的群']}]})
    with pytest.raises(ValueError):
        store.submit({})
    st.save_message({'msg_id': 'g9m', 'chat_id': 'g9', 'chat_type': 2, 'from_id': '2', 'content': 'x',
                     'create_time': int(time.time())}, chat_name='老群')
    p = store.submit(PLAN, trigger='init', source_ref='job:organize_init#3')
    kinds = [(i.seq, i.kind) for i in store.items(p.id)]
    assert kinds == [(1, 'group'), (2, 'group'), (3, 'label'), (4, 'label'), (5, 'unpin'), (6, 'pin'), (7, 'pin_order'),
                     (8, 'flag'), (9, 'mute')]
    text = store.render(p.id)
    assert '【初始整理方案 #1】 按业务分组' in text and '1. 建组「InfraBuddy」重点关注(2 个会话: 项目群、陈威)' in text
    assert '/ai 同意 1-5,8' in text and store.by_source('job:organize_init#3')[0].id == p.id


def test_parse_items():
    assert organize.parse_items('1-3,5', 6) == [1, 2, 3, 5]
    assert organize.parse_items('全部', 3) == [1, 2, 3]
    assert organize.parse_items('2、4', 4) == [2, 4]
    with pytest.raises(ValueError):
        organize.parse_items('9', 3)


def test_apply_subset_and_undo(st):
    st.save_message({'msg_id': 'g9m', 'chat_id': 'g9', 'chat_type': 2, 'from_id': '2', 'content': 'x',
                     'create_time': int(time.time())}, chat_name='老群')
    fs, store = FakeFeishu(), organize.OrganizeStore(st)
    ops = organize.FeishuOps(fs)
    fs.labels['500'] = ('InfraBuddy', ['99'])                               # 已有同名标签: 按并集重建
    p = store.submit(PLAN)
    res = store.apply(p.id, [1, 2, 3, 5, 6, 7, 8, 9], ops)
    assert all(r == '已执行' for _, r in res) and len(res) == 8
    cs = groups.ChatStore(st)
    v = {r['chat_id']: r for r in cs.view()}
    assert (v['11']['group'], v['11']['attention'], v['14']['attention'], v['12']['attention']) == \
        ('InfraBuddy', 'focus', 'focus', 'archive')
    assert sorted(n for n, _ in fs.labels.values()) == ['InfraBuddy'] and \
        sorted(next(iter(fs.labels.values()))[1]) == ['11', '14', '99']
    assert cs.group('InfraBuddy').feishu_label_id in fs.labels
    assert fs.pins == ['p0', '11'] and '777' in fs.flags and fs.remind['13'] is False
    assert store.get(p.id).status == 'pending'                            # 第 4 条还没定
    assert store.reject(p.id, [4]) == 1 and store.get(p.id).status == 'done'
    assert store.apply(p.id, [1], ops) == []                              # 执行过的不重复
    undone = store.undo(p.id, ops)
    assert len(undone) == 8 and all(r == '已撤销' for _, r in undone)
    assert fs.pins == ['p0', 'g9'] and '777' not in fs.flags and fs.remind['13'] is True
    assert [(n, sorted(m)) for n, m in fs.labels.values()] == [('InfraBuddy', ['99'])]
    v = {r['chat_id']: r for r in cs.view()}
    assert v['11']['group'] == '' and not v['11']['confirmed'] and cs.group('InfraBuddy') is None


def _args(**kw):
    return argparse.Namespace(**dict(dict(action='show', target=None, items='', limit=20, json=False), **kw))


def test_cli_permissions_and_flow(st, monkeypatch, tmp_path, capsys):
    fs = FakeFeishu()
    monkeypatch.setattr(cli, 'Storage', lambda: st)
    monkeypatch.setattr(cli, 'LarkClient', lambda auth: fs)
    monkeypatch.setattr(cli, 'LarkAuth', lambda: None)
    f = tmp_path / 'plan.json'
    f.write_text(json.dumps({'mute': ['大群']}, ensure_ascii=False), encoding='utf-8')
    monkeypatch.setenv('LARKX_ACTOR', 'agent')
    monkeypatch.setattr(state, 'read_turn', lambda: {'kind': 'system', 'chat_id': 'self', 'is_owner': False,
                                                     'ref': 'job:review#1'})
    assert cli.cmd_organize(_args(action='submit', target=str(f))) == 3     # 回顾里不能提交
    monkeypatch.setattr(state, 'read_turn', lambda: {'kind': 'system', 'chat_id': 'self', 'is_owner': False,
                                                     'ref': 'job:organize#2'})
    assert cli.cmd_organize(_args(action='submit', target=str(f))) == 0
    assert '【每周整理方案 #1】' in capsys.readouterr().out
    assert cli.cmd_organize(_args(action='apply')) == 3                     # 维护任务里不能执行
    assert cli.cmd_organize(_args(action='show')) == 0
    monkeypatch.setattr(state, 'read_turn', lambda: {'kind': 'message', 'chat_id': 'self', 'is_owner': True, 'ref': 'm1'})
    assert cli.cmd_organize(_args(action='apply', items='1')) == 0          # 本人在私聊说 "/ai 同意 1"
    assert fs.remind['13'] is False
    assert cli.cmd_organize(_args(action='undo', target='1')) == 0
    assert fs.remind['13'] is True
    capsys.readouterr()
    assert cli.cmd_organize(_args(action='list', json=True)) == 0
    assert json.loads(capsys.readouterr().out)[0]['items']['undone'] == 1


def test_daemon_sends_proposal(tmp_path, monkeypatch, fake_client):
    from larkx.agent import TurnResult
    from tests.test_reliability import _daemon
    obj = _daemon(tmp_path, fake_client, monkeypatch)
    for cid, name in (('13', '大群'),):
        obj.storage.save_message({'msg_id': 'x', 'chat_id': cid, 'chat_type': 2, 'from_id': '2', 'content': 'x',
                                  'create_time': int(time.time())}, chat_name=name)
    run = obj.maintenance.store.create('organize', '2026-11-09')
    organize.OrganizeStore(obj.storage).submit({'mute': ['大群']}, trigger='weekly', source_ref=run.ref)
    obj.maintenance.on_turn_done(type('E', (), {'run_id': run.id, 'chat_id': 'self'})(),
                                 TurnResult('success', False, '提了 1 条', 1.0))
    assert any('【每周整理方案 #1】' in t for c, t in fake_client.sent if c == 'self')
    assert obj.maintenance.store.get(run.id).status == 'ok'
    run2 = obj.maintenance.store.create('organize', '2026-11-16')                       # 这周不用调整
    obj.maintenance.on_turn_done(type('E', (), {'run_id': run2.id, 'chat_id': 'self'})(),
                                 TurnResult('success', False, '本周不需要调整', 1.0))
    assert obj.maintenance.store.get(run2.id).summary.startswith('没有提交方案')


def test_organize_prompt_has_context(st, fake_client):
    from datetime import datetime
    from larkx.daemon import prompts
    fake_client.api_responses = {'feed.PullShortcutsRequest': {'shortcuts': [{'id': '11'}]},
                                 'feed.PullFeedGroupsRequest': {'feedGroups': []}}
    text = prompts.BUILDERS['organize_init']({}, st, datetime.now(), OWNER, client=fake_client)
    assert '初始整理会话' in text and '项目群 | 11 |' in text and '飞书置顶(按顺序): 项目群' in text
    assert 'larkx organize submit downloads/organize.json' in text


def test_label_keeps_local_members_when_remote_read_empty(st):
    """读标签成员的接口读回空时, 重建标签不能丢掉组里原有的会话。"""
    fs, store = FakeFeishu(), organize.OrganizeStore(st)
    ops = organize.FeishuOps(fs)
    cs = groups.ChatStore(st)
    g = cs.create_group('InfraBuddy', 'focus')
    cs.assign(['13'], group_id=g.id)
    fs.labels['500'] = ('InfraBuddy', ['13'])
    cs.update_group(g.id, feishu_label_id='500')
    ops.label_items = lambda lid: []                                    # 接口读不到
    p = store.submit({'groups': [{'name': 'InfraBuddy', 'attention': '重点关注', 'chats': ['项目群']}]})
    store.apply(p.id, [1, 2], ops)
    assert sorted(next(iter(fs.labels.values()))[1]) == ['11', '13']
    store.undo(p.id, ops)
    assert [sorted(m) for _, m in fs.labels.values()] == [['13']]
