"""整理方案: 分身提出一组改动, 本人同意哪几条就执行哪几条, 每条都能撤销。整个功能和每一类改动都是可选的:

- LARKX_ORGANIZE 总开关, 默认关; 关着时提交、执行、撤销的命令和网页接口都拒绝。
- LARKX_ORGANIZE_ITEMS 选提哪几类: 分类(本地)、置顶(含取消、排序)、免打扰、标记、标签(飞书标签)。
  默认只有分类、置顶、免打扰; 没打开的类型既不提也不执行。

改动类型(执行顺序): 分类 -> 飞书标签 -> 取消置顶 -> 置顶 -> 置顶排序 -> 标记消息 -> 免打扰。
飞书上的改动只影响本人自己的客户端, 不对外发消息。执行前记下原来的状态, 撤销时还原;
本人执行之后又手动改过的, 撤销时不覆盖。飞书标签只管本程序建的, 用户自己的标签(包括同名的)一律不动。

方案由数字人写成 JSON 交给 larkx organize submit(打开后先提一次, 之后每周一次, 本人也可以随时要),
daemon 把编号列表私信给本人; 本人在私聊回 "/ai 同意 #3 1-5,8" 之类, 数字人执行 larkx organize apply 3 --items 1-5,8。
"""
import json
import re
import time
import uuid
from datetime import datetime

from sqlalchemy import desc

from . import groups
from .storage.models import ChatGroup, ChatProfile, Proposal, ProposalItem

KIND_ORDER = ('group', 'label', 'unpin', 'pin', 'pin_order', 'flag', 'mute')
KIND_NAMES = {'group': '分类', 'label': '飞书标签', 'unpin': '取消置顶', 'pin': '置顶', 'pin_order': '置顶排序',
              'flag': '标记消息', 'mute': '免打扰'}
TRIGGER_NAMES = {'init': '初始整理', 'weekly': '每周整理', 'manual': '整理'}
# LARKX_ORGANIZE_ITEMS 里的名字 -> 改动类型
# 「标记」(flag)先不开放: 还没实测 UpdateFlagsRequest 会不会顺带把别人的消息标成已读, 不能标已读是硬约束。
# 实测确认后再加上 '标记': ('flag',)
ITEM_KINDS = {'分类': ('group',), '置顶': ('pin', 'unpin', 'pin_order'), '免打扰': ('mute',), '标签': ('label',)}


ITEM_NAMES = {'group': '分类', 'pin': '置顶', 'mute': '免打扰', 'label': '标签'}


def enabled(cfg: dict) -> bool:
    return bool(cfg.get('organize') and cfg.get('chat_groups'))


def enabled_kinds(cfg: dict) -> set:
    """打开了哪几类改动; 写错的名字忽略。"""
    names = [n.strip() for n in str(cfg.get('organize_items') or '').replace('，', ',').split(',') if n.strip()]
    return {k for n in names for k in ITEM_KINDS.get(n, ())}


def kind_label(kind: str) -> str:
    return next((n for n, ks in ITEM_KINDS.items() if kind in ks), '标记' if kind == 'flag' else kind)


class Skip(Exception):
    """这一条不执行(比如有用户自己建的同名标签), 记为跳过, 不算失败。"""


class NotYet(Exception):
    """这一条现在还不能执行(比如要先执行建分类的那一条), 留着待定, 之后可以再执行。"""


class FeishuOps:
    """本人飞书客户端状态的读写(置顶、标记、免打扰、标签), 接口都在本人账号上实测过。"""

    def __init__(self, client):
        self.c = client

    def shortcuts(self) -> list:
        """侧边栏全部置顶(会话、文档等), 按顺序: [{'id', 'type'}]。position 不是翻页偏移, 拿不全时用大 count 再要一次。"""
        resp = self.c.api('feed.PullShortcutsRequest', {}) or {}
        if resp.get('hasMore'):
            resp = self.c.api('feed.PullShortcutsRequest', {'count': 1000}) or {}
            if resp.get('hasMore'):
                raise RuntimeError('置顶太多, 一次读不全, 不改置顶')
        return [{'id': str(x['id']), 'type': x.get('type', 1)} for x in resp.get('shortcuts') or []]

    def pins(self) -> list:
        """置顶的会话(type=1), 按顺序。"""
        return [x['id'] for x in self.shortcuts() if x['type'] == 1]

    def pin(self, ids):
        if ids:
            self.c.api('feed.PutShortcutsRequest', {'shortcuts': [{'id': str(i), 'type': 1} for i in ids]})

    def unpin(self, ids):
        if ids:
            self.c.api('feed.DeleteShortcutsRequest', {'shortcuts': [{'id': str(i), 'type': 1} for i in ids]})

    def order(self, ids):
        """把置顶的会话按 ids 排序; 文档等非会话的置顶留在原来的位置, 类型原样提交。"""
        cur = self.shortcuts()
        chats = [i for i in ids if any(x['id'] == i and x['type'] == 1 for x in cur)]
        chats += [x['id'] for x in cur if x['type'] == 1 and x['id'] not in chats]
        it = iter(chats)
        full = [{'id': next(it), 'type': 1} if x['type'] == 1 else x for x in cur]
        self.c.api('feed.PatchShortcutsRequest',
                   {'shortcuts': [{'id': x['id'], 'type': x['type'], 'position': n} for n, x in enumerate(full)]})

    def flags(self) -> set:
        return {str(i['itemId']) for i in (self.c.api('feed.PullFlagsRequest', {'count': 50}) or {}).get('items') or []}

    def flag(self, msg_ids, on=True):
        now = int(time.time() * 1000)
        items = [{'itemId': int(m), 'itemType': 0, 'flagType': 2, 'createTime': now, 'updateTime': now} for m in msg_ids]
        if items:
            self.c.api('feed.UpdateFlagsRequest', {'items': items} if on else {'deletedItems': items})

    def remind(self, chat_ids) -> dict:
        out = {}
        for i in range(0, len(chat_ids), 20):
            chats = (self.c.api('chats.PullChatsByIdsRequest', {'chatIds': list(chat_ids[i:i + 20])}) or {}).get('chats') or {}
            out.update({str(k): (v.get('userSetting') or {}).get('isRemind', v.get('isRemind')) for k, v in chats.items()})
        return out

    def mute(self, chat_ids, on=True):
        if chat_ids:
            self.c.api('feed.BatchMuteFeedCardsRequest', {
                'taskId': str(uuid.uuid4()), 'batchId': str(uuid.uuid4()), 'unmute': not on, 'muteType': [1],
                'feedCards': [{'id': str(i), 'type': 1} for i in chat_ids]})

    def labels(self) -> dict:
        """全部飞书标签 {id: 名字}, 翻页读完(一页最多 20 个, 再大接口报 400)。"""
        out, cursor = {}, ''
        for _ in range(50):
            resp = self.c.api('feed.PullFeedGroupsRequest', {'count': 20, 'cursor': cursor, 'version': 1}) or {}
            out.update({str(g['id']): g.get('name', '') for g in resp.get('feedGroups') or []})
            nxt = resp.get('nextCursor') or ''
            if not resp.get('hasMore') or not nxt or nxt == cursor:
                break
            cursor = nxt
        return out

    def create_label(self, name, chat_ids) -> str:
        r = self.c.api('feed.CreateFeedGroupRequest', {'feedGroup': {'type': 1, 'name': name},
                       'feedGroupItems': [{'feedCardId': int(i), 'feedCardType': 1} for i in chat_ids]}) or {}
        return str((r.get('feedGroup') or {}).get('id') or '')

    def delete_label(self, label_id):
        self.c.api('feed.UpdateFeedGroupsRequest', {'deletedGroups': [{'id': int(label_id), 'type': 1}]})


def parse_items(text: str, total: int) -> list:
    """'1-5,8' / '全部' -> [1,2,3,4,5,8]。必须写明, 空着不算全部。"""
    text = (text or '').strip()
    if not text:
        raise ValueError('要写明第几条, 如 --items 1-5,8; 全部执行写 --items 全部')
    if text in ('all', '全部'):
        return list(range(1, total + 1))
    out = set()
    for part in re.split(r'[,，、\s]+', text):
        if not part:
            continue
        m = re.fullmatch(r'(\d+)\s*[-~到]\s*(\d+)', part)
        if m:
            out.update(range(int(m.group(1)), int(m.group(2)) + 1))
        elif part.isdigit():
            out.add(int(part))
        else:
            raise ValueError(f'看不懂编号 {part!r}, 例: 1-5,8')
    bad = [n for n in out if not 1 <= n <= total]
    if bad:
        raise ValueError(f'没有第 {bad[0]} 条(共 {total} 条)')
    return sorted(out)


class OrganizeStore:
    def __init__(self, storage, kinds=None):
        """kinds: 打开的改动类型(见 enabled_kinds); None 表示不限制(测试和查看用)。"""
        self.storage = storage
        self.Session = storage.Session
        self.chats = groups.ChatStore(storage)
        self.kinds = set(KIND_ORDER) if kinds is None else set(kinds)

    # ---- 提交 ----

    def _chat(self, ref, raw_ok=False) -> str:
        """会话名或 chat_id -> chat_id。raw_ok: 置顶里可能有本地没记录的老会话, 原样接受 id。"""
        found = self.chats.find_chats(str(ref))
        if not found and raw_ok and re.fullmatch(r'[\w-]+', str(ref)):
            return str(ref)
        if len(found) != 1:
            raise ValueError(f'会话「{ref}」' + ('找不到' if not found else f'对上 {len(found)} 个, 请写 chat_id'))
        return found[0][0]

    def _names(self, ids, limit=8) -> str:
        names = {r['chat_id']: r['name'] or r['chat_id'] for r in self.chats.view(ids)}
        shown = [names.get(i, i) for i in ids[:limit]]
        return '、'.join(shown) + (f' 等 {len(ids)} 个' if len(ids) > limit else '')

    def submit(self, plan: dict, trigger: str = 'manual', source_ref: str = '') -> Proposal:
        """把数字人写的方案(JSON)校验后存成编号的条目。会话可以写名字或 chat_id。"""
        att = groups.ATTENTION
        items = []
        for g in plan.get('groups') or []:
            ids = [self._chat(c) for c in g.get('chats') or []]
            a = groups.parse_attention(g.get('attention') or 'light')
            params = {'name': g['name'].strip(), 'attention': a, 'private': bool(g.get('private')),
                      'keywords': list(g.get('keywords') or []), 'chat_ids': ids}
            exist = self.chats.group(params['name'])
            verb = '调整分类' if exist else '建分类'
            items.append(('group', f"{verb}「{params['name']}」{att[a]}" + ('、私人' if params['private'] else '')
                          + (f"({len(ids)} 个会话: {self._names(ids)})" if ids else ''), g.get('reason', ''), params))
            if plan.get('labels', True) and ids:
                items.append(('label', f"飞书标签「{params['name']}」放入 {len(ids)} 个会话", '和分类对应, 在飞书里按标签看',
                              {'group': params['name'], 'chat_ids': ids}))
        for kind in ('unpin', 'pin'):
            for p in plan.get(kind) or []:
                cid = self._chat(p['chat'] if isinstance(p, dict) else p, raw_ok=kind == 'unpin')
                items.append((kind, f"{KIND_NAMES[kind]}「{self._names([cid])}」", (p.get('reason', '') if isinstance(p, dict) else ''),
                              {'chat_id': cid}))
        if plan.get('pin_order'):
            ids = [self._chat(c, raw_ok=True) for c in plan['pin_order']]
            items.append(('pin_order', f'置顶按分类排序({len(ids)} 个): {self._names(ids, 5)}', '', {'chat_ids': ids}))
        for f in plan.get('flag') or []:
            cid = self._chat(f['chat'])
            items.append(('flag', f"标记「{self._names([cid])}」的消息: {str(f.get('text', ''))[:40]}", f.get('reason', ''),
                          {'msg_id': str(f['msg_id']), 'chat_id': cid}))
        mutes = [self._chat(m['chat'] if isinstance(m, dict) else m) for m in plan.get('mute') or []]
        if mutes:
            items.append(('mute', f'免打扰 {len(mutes)} 个会话: {self._names(mutes)}', plan.get('mute_reason', ''),
                          {'chat_ids': mutes}))
        off = sorted({kind_label(k) for k, *_ in items if k not in self.kinds})
        items = [x for x in items if x[0] in self.kinds]
        if not items:
            raise ValueError('方案里没有任何改动' + (f'(没打开的类型: {"、".join(off)})' if off else ''))
        items.sort(key=lambda x: KIND_ORDER.index(x[0]))
        s = self.Session()
        try:
            summary = str(plan.get('summary') or '') + (f'(没打开的{"、".join(off)}类改动已去掉)' if off else '')
            prop = Proposal(trigger=trigger, source_ref=source_ref, summary=summary)
            s.add(prop)
            s.flush()
            for n, (kind, title, reason, params) in enumerate(items, start=1):
                s.add(ProposalItem(proposal_id=prop.id, seq=n, kind=kind, title=title, reason=reason or '',
                                   params=json.dumps(params, ensure_ascii=False)))
            s.commit()
            return prop
        finally:
            s.close()

    # ---- 查看 ----

    def get(self, proposal_id: int = None):
        s = self.Session()
        try:
            if proposal_id:
                return s.get(Proposal, proposal_id)
            return s.query(Proposal).order_by(desc(Proposal.id)).first()
        finally:
            s.close()

    def items(self, proposal_id: int) -> list:
        s = self.Session()
        try:
            return s.query(ProposalItem).filter(ProposalItem.proposal_id == proposal_id).order_by(ProposalItem.seq).all()
        finally:
            s.close()

    def list(self, limit=20) -> list:
        s = self.Session()
        try:
            return s.query(Proposal).order_by(desc(Proposal.id)).limit(limit).all()
        finally:
            s.close()

    def by_source(self, ref: str) -> list:
        s = self.Session()
        try:
            return s.query(Proposal).filter(Proposal.source_ref == ref).all()
        finally:
            s.close()

    def _set(self, model, row_id, **fields):
        s = self.Session()
        try:
            row = s.get(model, row_id)
            for k, v in fields.items():
                setattr(row, k, v)
            s.commit()
        finally:
            s.close()

    def mark_notified(self, proposal_id):
        self._set(Proposal, proposal_id, notified=1)

    def render(self, proposal_id: int) -> str:
        prop = self.get(proposal_id)
        lines = [f"【{TRIGGER_NAMES.get(prop.trigger, '整理')}方案 #{prop.id}】" + (f" {prop.summary}" if prop.summary else '')]
        last = None
        for it in self.items(proposal_id):
            if it.kind != last:
                lines.append(f'\n{KIND_NAMES[it.kind]}')
                last = it.kind
            mark = {'applied': ' ✅', 'failed': ' ❌', 'rejected': ' ✖', 'undone': ' ↩', 'skipped': ' ⏭'}.get(it.status, '')
            lines.append(f'{it.seq}. {it.title}' + (f' —— {it.reason}' if it.reason else '') + mark)
        if prop.status == 'pending':
            lines.append(f'\n在这个私聊回「/ai 同意 #{prop.id} 全部」或「/ai 同意 #{prop.id} 1-5,8」执行, '
                         f'「/ai 不要 #{prop.id} 3」放弃某条; 执行后可以「/ai 撤销整理 #{prop.id}」。'
                         f'只改你自己的飞书客户端, 不会给别人发消息。')
        return '\n'.join(lines)

    # ---- 执行与撤销 ----

    def apply(self, proposal_id: int, seqs: list, ops: FeishuOps, by: str = 'cli') -> list:
        """执行选中的条目(已执行的跳过), 返回 [(编号, 结果)]。每条单独成败, 失败不影响其他条。"""
        prop = self.get(proposal_id)
        if not prop:
            raise ValueError(f'没有整理方案 #{proposal_id}')
        out = []
        for it in self.items(proposal_id):
            if it.seq not in seqs or it.status in ('applied', 'undone', 'skipped'):
                continue
            if it.kind not in self.kinds:
                out.append((it.seq, f'没打开「{kind_label(it.kind)}」这类改动, 不执行'))
                continue
            params = json.loads(it.params or '{}')
            try:
                snap = getattr(self, f'_apply_{it.kind}')(params, ops)
                self._set(ProposalItem, it.id, status='applied', snapshot=json.dumps(snap, ensure_ascii=False),
                          applied_at=datetime.now(), error='')
                out.append((it.seq, '已执行'))
            except Skip as e:
                self._set(ProposalItem, it.id, status='skipped', error=str(e)[:500])
                out.append((it.seq, f'跳过: {e}'))
            except NotYet as e:
                self._set(ProposalItem, it.id, error=str(e)[:500])
                out.append((it.seq, f'暂不执行: {e}'))
            except Exception as e:
                self._set(ProposalItem, it.id, status='failed', error=repr(e)[:500])
                out.append((it.seq, f'失败: {e}'))
        self._settle(proposal_id)
        return out

    def reject(self, proposal_id: int, seqs: list) -> int:
        n = 0
        for it in self.items(proposal_id):
            if it.seq in seqs and it.status == 'pending':
                self._set(ProposalItem, it.id, status='rejected')
                n += 1
        self._settle(proposal_id)
        return n

    def undo(self, proposal_id: int, ops: FeishuOps) -> list:
        """撤销已执行的条目, 倒序还原; 执行之后本人又手动改过的不覆盖。"""
        out = []
        for it in reversed(self.items(proposal_id)):
            if it.status != 'applied':
                continue
            try:
                getattr(self, f'_undo_{it.kind}')(json.loads(it.params or '{}'), json.loads(it.snapshot or '{}'), ops)
                self._set(ProposalItem, it.id, status='undone')
                out.append((it.seq, '已撤销'))
            except Exception as e:
                out.append((it.seq, f'撤销失败: {e}'))
        return sorted(out)

    def _settle(self, proposal_id):
        left = [it for it in self.items(proposal_id) if it.status == 'pending']
        if not left:
            self._set(Proposal, proposal_id, status='done')

    # 分类(本地)
    def _apply_group(self, p, ops):
        g = self.chats.group(p['name'])
        snap = {'created': not g, 'group': None if not g else
                {'attention': g.attention, 'private': g.private, 'keywords': g.keywords}}
        if g:
            self.chats.update_group(g.id, attention=p['attention'], private=1 if p['private'] else 0,
                                    keywords=p['keywords'])
        else:
            g = self.chats.create_group(p['name'], p['attention'], p['keywords'], p['private'])
        snap['group_id'] = g.id
        snap['chats'] = {}
        for cid in p['chat_ids']:
            pr = self.chats.profile(cid)
            snap['chats'][cid] = [pr.group_id, pr.attention, pr.confirmed] if pr else None
        self.chats.assign(p['chat_ids'], group_id=g.id, attention='')
        return snap

    def _undo_group(self, p, snap, ops):
        s = self.Session()
        try:
            for cid, old in snap['chats'].items():
                row = s.get(ChatProfile, cid)
                if row is None or row.group_id != snap['group_id']:
                    continue  # 执行之后本人又改过这个会话的分类: 不动
                if old is None:
                    row.group_id, row.attention, row.confirmed = 0, '', 0
                else:
                    row.group_id, row.attention, row.confirmed = old
            if snap['created'] and not s.query(ChatProfile).filter(ChatProfile.group_id == snap['group_id']).count():
                s.query(ChatGroup).filter(ChatGroup.id == snap['group_id']).delete()
            elif snap['group']:
                g = s.get(ChatGroup, snap['group_id'])
                g.attention, g.private, g.keywords = snap['group']['attention'], snap['group']['private'], snap['group']['keywords']
            s.commit()
        finally:
            s.close()

    # 飞书标签跟着分类走, 只管本程序建的(记在分类的 feishu_label_id 里)。成员以本地分类为准: 读标签成员的接口不可靠,
    # 往已有标签里加会话也不支持, 所以自己的标签删掉按"本地分类的全部会话 + 方案里的"重建。
    # 用户自己的标签(包括同名的)一律不动: 有同名的就跳过这一条
    def _apply_label(self, p, ops):
        g = self.chats.group(p['group'])
        if not g:  # 分类那一条没执行: 先不建标签, 免得之后被当成用户自己的同名标签、永远跳过
            raise NotYet(f'分类「{p["group"]}」还没建, 先执行建分类的那一条')
        labels = ops.labels()
        own = g.feishu_label_id if g.feishu_label_id in labels else ''
        if not own and p['group'] in labels.values():
            raise Skip(f'飞书里已有你自己建的标签「{p["group"]}」, 不动它; 要合并请自己在飞书里加会话')
        local = [r['chat_id'] for r in self.chats.view() if r['group_id'] == g.id]
        old_members = [c for c in local if c not in p['chat_ids']] if own else []
        members = list(dict.fromkeys([*local, *p['chat_ids']]))
        if own:
            ops.delete_label(own)
            self.chats.update_group(g.id, feishu_label_id='')  # 先清掉, 新建失败时不会指着已删的标签
        new_id = ops.create_label(p['group'], members)
        self.chats.update_group(g.id, feishu_label_id=new_id)
        return {'old_id': own, 'old_members': old_members, 'new_id': new_id}

    def _undo_label(self, p, snap, ops):
        labels = ops.labels()
        if snap.get('new_id') in labels:
            ops.delete_label(snap['new_id'])
        restored = ops.create_label(p['group'], snap['old_members']) if snap.get('old_id') else ''
        g = self.chats.group(p['group'])
        if g:
            self.chats.update_group(g.id, feishu_label_id=restored)

    def _apply_pin(self, p, ops):
        had = p['chat_id'] in ops.pins()
        if not had:
            ops.pin([p['chat_id']])
        return {'had': had}

    def _undo_pin(self, p, snap, ops):
        if not snap.get('had') and p['chat_id'] in ops.pins():
            ops.unpin([p['chat_id']])

    def _apply_unpin(self, p, ops):
        before = ops.pins()
        if p['chat_id'] in before:
            ops.unpin([p['chat_id']])
        return {'had': p['chat_id'] in before, 'index': before.index(p['chat_id']) if p['chat_id'] in before else -1}

    def _undo_unpin(self, p, snap, ops):
        if not snap.get('had') or p['chat_id'] in ops.pins():
            return  # 本来就没置顶, 或者本人已经重新置顶了
        ops.pin([p['chat_id']])
        cur = [c for c in ops.pins() if c != p['chat_id']]
        idx = snap.get('index', -1)
        if 0 <= idx < len(cur):  # 放回原来的位置(新置顶会排到最后)
            ops.order(cur[:idx] + [p['chat_id']] + cur[idx:])

    def _apply_pin_order(self, p, ops):
        before = ops.pins()
        want = [c for c in p['chat_ids'] if c in before] + [c for c in before if c not in p['chat_ids']]
        ops.order(want)
        return {'before': before}

    def _undo_pin_order(self, p, snap, ops):
        cur = ops.pins()
        ops.order([c for c in snap['before'] if c in cur] + [c for c in cur if c not in snap['before']])

    def _apply_flag(self, p, ops):
        had = p['msg_id'] in ops.flags()
        if not had:
            ops.flag([p['msg_id']])
        return {'had': had}

    def _undo_flag(self, p, snap, ops):
        if not snap.get('had') and p['msg_id'] in ops.flags():
            ops.flag([p['msg_id']], on=False)

    def _apply_mute(self, p, ops):
        """只给读得到"现在会提醒"的会话设免打扰; 读不到状态的跳过, 撤销时也不会误解除本来就免打扰的。"""
        before = ops.remind(p['chat_ids'])
        todo = [c for c in p['chat_ids'] if before.get(c) is True]
        if not todo:
            raise Skip('这些会话已经免打扰, 或者读不到提醒状态')
        ops.mute(todo)
        return {'muted': todo}

    def _undo_mute(self, p, snap, ops):
        now = ops.remind(snap.get('muted') or [])
        ops.mute([c for c in snap.get('muted') or [] if now.get(c) is False], on=False)  # 本人又解除了的不动


CONTEXT_CHATS = 150


def context(storage, client, owner_id: str, kinds=None) -> str:
    """给数字人写方案用的现状: 全部会话(信号、当前分类和建议)、已有的分类、飞书置顶顺序和标签、找本人还没回的消息。"""
    from . import digest
    cs = groups.ChatStore(storage)
    ops = FeishuOps(client) if client else None
    lines = ['会话(名字 | chat_id | 类型 | 人数 | 30天消息/本人发言/@本人 | 置顶/免打扰 | 当前分类(关注度, 来源) | 画像建议):']
    rows = sorted(cs.view(), key=lambda r: -(r['msgs'] or 0))
    if len(rows) > CONTEXT_CHATS:  # 太多时只列活跃的和还没分类的, 免得提示词过长
        rows = [r for r in rows if r['msgs'] or not r['group']][:CONTEXT_CHATS]
    for r in rows:
        kind = {1: '私聊' + ('·机器人' if r['peer_bot'] else ''), 2: '群', 3: '话题群'}.get(r['chat_type'], '?')
        flags = '/'.join(x for x, on in (('置顶', r['feishu_pinned']), ('免打扰', r['feishu_muted'])) if on) or '-'
        lines.append(f"- {r['name'] or '(未命名)'} | {r['chat_id']} | {kind} | {r['members'] or ''} | "
                     f"{r['msgs']}/{r['owner_msgs']}/{r['at_me']} | {flags} | "
                     f"{r['group'] or '未分类'}({r['attention_name']}, {r['attention_source']}) | "
                     f"{r['suggested_group']}/{groups.ATTENTION.get(r['suggested_attention'], '')}: {r['reason']}")
    gs = cs.groups()
    lines.append('\n已有的分类: ' + ('; '.join(f"{g.name}({groups.ATTENTION[g.attention]}{'、私人' if g.private else ''})"
                                         for g in gs) or '还没有'))
    kinds = set(KIND_ORDER) if kinds is None else set(kinds)
    if ops:
        try:
            names = {r['chat_id']: r['name'] for r in cs.view()}
            if kinds & {'pin', 'unpin', 'pin_order'}:
                lines.append('飞书置顶(按顺序): ' + '、'.join(names.get(c, c) for c in ops.pins()))
            if 'label' in kinds:
                lines.append('飞书标签: ' + ('、'.join(ops.labels().values()) or '没有'))
        except Exception as e:
            lines.append(f'(读飞书置顶/标签失败: {e!r})')
    data = digest.briefing_data(storage, owner_id, client=client) if 'flag' in kinds else {'unreplied': []}
    if data['unreplied']:
        lines.append('\n最近找本人还没回的消息(可以建议标记, flag 里要写 msg_id, 用 larkx messages <chat_id> 查):')
        lines += [f"- {u['time']} 「{u['chat_name']}」{u['sender_name']}: {u['content']}" for u in data['unreplied']]
    return '\n'.join(lines)
