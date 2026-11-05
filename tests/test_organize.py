"""整理方案: 开关和每类改动可选、提交校验、编号、按条执行、撤销还原(不覆盖之后的手动改动)、只管自己建的标签、权限、私信本人。"""
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
        self.types = {}  # 置顶里非会话的(文档等): id -> type

    def api(self, name, d=None):
        d = d or {}
        if name == 'feed.PullShortcutsRequest':
            return {'shortcuts': [{'id': i, 'type': self.types.get(i, 1)} for i in self.pins], 'hasMore': False}
        if name == 'feed.PutShortcutsRequest':
            self.pins += [s['id'] for s in d['shortcuts'] if s['id'] not in self.pins]
        elif name == 'feed.DeleteShortcutsRequest':
            gone = {s['id'] for s in d['shortcuts']}
            self.pins = [i for i in self.pins if i not in gone]
        elif name == 'feed.PatchShortcutsRequest':
            assert all(s['type'] == self.types.get(s['id'], 1) for s in d['shortcuts'])    # 类型原样提交
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
    for cid, name in (('11', '项目群'), ('12', '测试群'), ('13', '大群'), ('14', '赵六')):
        s.save_message({'msg_id': f'm{cid}', 'chat_id': cid, 'chat_type': 2, 'from_id': '200', 'content': 'x',
                        'create_time': int(time.time())}, chat_name=name)
    return s


PLAN = {'summary': '按业务分类',
        'groups': [{'name': '项目A', 'attention': '重点关注', 'keywords': ['发布'], 'chats': ['项目群', '14'],
                    'reason': '主业务'},
                   {'name': '测试', 'attention': '只存不看', 'chats': ['测试群']}],
        'pin': [{'chat': '项目群', 'reason': '常用'}], 'unpin': [{'chat': 'g9', 'reason': '沉寂'}],
        'pin_order': ['p0', '项目群'],
        'flag': [{'chat': '赵六', 'msg_id': '777', 'text': '等你上线', 'reason': '待跟进'}],
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
    assert '【初始整理方案 #1】 按业务分类' in text and '1. 建分类「项目A」重点关注(2 个会话: 项目群、赵六)' in text
    assert '/ai 同意 #1 1-5,8' in text and store.by_source('job:organize_init#3')[0].id == p.id


def test_parse_items():
    assert organize.parse_items('1-3,5', 6) == [1, 2, 3, 5]
    assert organize.parse_items('全部', 3) == [1, 2, 3]
    assert organize.parse_items('2、4', 4) == [2, 4]
    with pytest.raises(ValueError):
        organize.parse_items('9', 3)
    with pytest.raises(ValueError):                                  # 不写不算全部
        organize.parse_items('', 3)


def test_apply_subset_and_undo(st):
    st.save_message({'msg_id': 'g9m', 'chat_id': 'g9', 'chat_type': 2, 'from_id': '2', 'content': 'x',
                     'create_time': int(time.time())}, chat_name='老群')
    fs, store = FakeFeishu(), organize.OrganizeStore(st)
    ops = organize.FeishuOps(fs)
    fs.labels['500'] = ('测试', ['99'])                                # 本人自己建的同名标签: 不动, 跳过
    p = store.submit(PLAN)
    res = dict(store.apply(p.id, [1, 2, 3, 4, 5, 6, 7, 8, 9], ops))
    assert res[4].startswith('跳过') and all(r == '已执行' for k, r in res.items() if k != 4)
    assert fs.labels['500'] == ('测试', ['99'])
    cs = groups.ChatStore(st)
    v = {r['chat_id']: r for r in cs.view()}
    assert (v['11']['group'], v['11']['attention'], v['14']['attention'], v['12']['attention']) == \
        ('项目A', 'focus', 'focus', 'archive')
    own = cs.group('项目A').feishu_label_id
    assert fs.labels[own] == ('项目A', ['11', '14'])
    assert fs.pins == ['p0', '11'] and '777' in fs.flags and fs.remind['13'] is False
    assert store.get(p.id).status == 'done'
    assert store.apply(p.id, [1], ops) == []                              # 执行过的不重复
    undone = store.undo(p.id, ops)
    assert len(undone) == 8 and all(r == '已撤销' for _, r in undone)
    assert fs.pins == ['p0', 'g9'] and '777' not in fs.flags and fs.remind['13'] is True
    assert list(fs.labels.values()) == [('测试', ['99'])]                 # 自己建的删了, 用户的还在
    v = {r['chat_id']: r for r in cs.view()}
    assert v['11']['group'] == '' and not v['11']['confirmed'] and cs.group('项目A') is None


def test_undo_keeps_changes_made_afterwards(st):
    """执行之后本人又手动改过的, 撤销时不覆盖; 取消置顶撤销后放回原来的位置。"""
    fs, store = FakeFeishu(), organize.OrganizeStore(st)
    ops = organize.FeishuOps(fs)
    fs.pins = ['p0', '11', 'g9']
    p = store.submit({'unpin': ['11'], 'mute': ['大群', '测试群'], 'pin': ['赵六'],
                      'groups': [{'name': '项目A', 'chats': ['项目群', '赵六']}], 'labels': False})
    store.apply(p.id, organize.parse_items('全部', len(store.items(p.id))), ops)
    cs = groups.ChatStore(st)
    cs.assign(['14'], group_id=cs.create_group('别的', 'light').id)           # 本人之后把赵六挪走了
    fs.remind['12'] = True                                                   # 本人又解除了测试群的免打扰
    fs.pins.remove('14')                                                     # 本人自己取消了置顶
    store.undo(p.id, ops)
    assert fs.pins == ['p0', '11', 'g9']                                     # 放回原位
    assert cs.view(['14'])[0]['group'] == '别的' and fs.remind['13'] is True and fs.remind['12'] is True


def test_mute_unknown_state_skipped(st):
    """读不到提醒状态的会话不设免打扰, 撤销时也不会把本来免打扰的解除。"""
    fs, store = FakeFeishu(), organize.OrganizeStore(st)
    ops = organize.FeishuOps(fs)
    ops.remind = lambda ids: {c: None for c in ids}
    p = store.submit({'mute': ['大群']})
    assert store.apply(p.id, [1], ops)[0][1].startswith('跳过') and '13' not in fs.remind


def test_kinds_filter_submit_and_apply(st):
    store = organize.OrganizeStore(st, organize.enabled_kinds({'organize_items': '分类,免打扰'}))
    p = store.submit(PLAN)
    assert {i.kind for i in store.items(p.id)} == {'group', 'mute'} and '没打开' in store.get(p.id).summary
    with pytest.raises(ValueError, match='没打开的类型'):
        store.submit({'pin': ['项目群']})
    full = organize.OrganizeStore(st).submit({'pin': ['项目群']})                    # 之前打开时提交的
    assert '没打开「置顶」' in store.apply(full.id, [1], organize.FeishuOps(FakeFeishu()))[0][1]
    from larkx.config import DEFAULTS
    assert organize.enabled_kinds(DEFAULTS) == {'group', 'pin', 'unpin', 'pin_order', 'mute'}


def test_pin_order_keeps_non_chat_shortcuts(st):
    fs = FakeFeishu()
    fs.pins, fs.types = ['p0', 'doc1', 'g9', '11'], {'doc1': 4}
    organize.FeishuOps(fs).order(['11', 'p0'])
    assert fs.pins == ['11', 'doc1', 'p0', 'g9']                            # 文档留在原位, 会话按顺序排


def _args(**kw):
    return argparse.Namespace(**dict(dict(action='show', target=None, items='', limit=20, json=False), **kw))


def test_cli_permissions_and_flow(st, monkeypatch, tmp_path, capsys):
    from larkx import notify
    from larkx.config import DEFAULTS
    fs = FakeFeishu()
    monkeypatch.setattr(cli, 'load_config', lambda: dict(DEFAULTS))
    assert cli.cmd_organize(_args(action='show')) == 3                      # 默认关: 一律拒绝
    monkeypatch.setattr(cli, 'load_config', lambda: dict(DEFAULTS, chat_groups=True, organize=True))
    monkeypatch.setattr(notify, '_load', lambda: {'owner_chat': 'self'})
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
    assert cli.cmd_organize(_args(action='apply', target='1', items='1')) == 3   # 维护任务里不能执行
    assert cli.cmd_organize(_args(action='show')) == 0
    monkeypatch.setattr(state, 'read_turn', lambda: {'kind': 'message', 'chat_id': 'g1', 'is_owner': True, 'ref': 'm1'})
    assert cli.cmd_organize(_args(action='apply', target='1', items='1')) == 3   # 本人在群里说的也不行, 只认私聊
    monkeypatch.setattr(state, 'read_turn', lambda: {'kind': 'message', 'chat_id': 'self', 'is_owner': True, 'ref': 'm1'})
    assert cli.cmd_organize(_args(action='apply', items='1')) == 1          # 要写方案编号
    assert cli.cmd_organize(_args(action='apply', target='#1')) == 1        # 要写第几条
    assert cli.cmd_organize(_args(action='apply', target='#1', items='1')) == 0  # 本人在私聊说 "/ai 同意 #1 1"
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
    from larkx.config import DEFAULTS
    text = prompts.BUILDERS['organize_init'](dict(DEFAULTS), st, datetime.now(), OWNER, client=fake_client)
    assert '初始整理会话' in text and '项目群 | 11 |' in text and '飞书置顶(按顺序): 项目群' in text
    assert 'larkx organize submit downloads/organize.json' in text
    assert '"pin"' in text and '"mute"' in text and '"flag"' not in text and '飞书标签' not in text   # 默认三类
    full = prompts.BUILDERS['organize'](dict(DEFAULTS, organize_items='分类,置顶,免打扰,标记,标签'), st,
                                        datetime.now(), OWNER, client=fake_client)
    assert '"flag"' in full and '"labels": true' in full and '1. 分类' in full and '5. 免打扰' in full


def test_own_label_rebuilt_from_local_group(st):
    """本程序建的标签: 成员以本地分类为准(读标签成员的接口不可靠), 删掉按并集重建; 撤销时恢复成原来的成员。"""
    fs, store = FakeFeishu(), organize.OrganizeStore(st)
    ops = organize.FeishuOps(fs)
    cs = groups.ChatStore(st)
    g = cs.create_group('项目A', 'focus')
    cs.assign(['13'], group_id=g.id)
    fs.labels['500'] = ('项目A', ['13'])
    cs.update_group(g.id, feishu_label_id='500')
    p = store.submit({'groups': [{'name': '项目A', 'attention': '重点关注', 'chats': ['项目群']}]})
    store.apply(p.id, [1, 2], ops)
    assert sorted(next(iter(fs.labels.values()))[1]) == ['11', '13']
    store.undo(p.id, ops)
    assert [sorted(m) for _, m in fs.labels.values()] == [['13']]


def test_organize_jobs_off_by_default():
    from larkx import jobs
    from larkx.config import DEFAULTS
    for name in ('organize', 'organize_init'):
        assert jobs.get(name).when(dict(DEFAULTS)) is None
        assert jobs.get(name).when(dict(DEFAULTS, organize=True)) is None          # 要先打开会话分类
        assert jobs.get(name).when(dict(DEFAULTS, organize=True, chat_groups=True)) is not None
    assert jobs.get('organize').when(dict(DEFAULTS, organize=True, chat_groups=True)) == (4, 45, 0)


@pytest.mark.parametrize('backfill_days', [0, 30])
def test_organize_init_timing(tmp_path, monkeypatch, fake_client, backfill_days):
    """没回填: 第一次画像之后提; 回填了: 初始化记忆之后提。"""
    from larkx import jobs
    from tests.test_reliability import _daemon
    obj = _daemon(tmp_path, fake_client, monkeypatch)
    obj.cfg.update(chat_groups=True, organize=True, backfill_days=backfill_days, memory_review_hour=4,
                   memory_consolidate_weekday=-1)
    m, store = obj.maintenance, obj.maintenance.store
    job = jobs.get('organize_init')
    assert m._once_slot(job) is None                                            # 还没画像
    store.finish(store.create('profile', 'x').id, 'ok')
    if backfill_days:
        assert m._once_slot(job) is None
        store.put_state('bootstrap', {'done': True})
    assert m._once_slot(job) == 'once'
