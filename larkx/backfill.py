"""回填历史消息: 首次启动时把最近 N 天(默认 30)的聊天拉进本地库, 让分身一开始就了解本人的工作。

- 会话: 消息列表(feed)里 N 天内有更新的会话, 只读地拉, 不改变已读状态;
- 只拉补收核对位置(CatchupMark)以下的消息: 位置以上归补收和长连接管, 那里会照常判断要不要交给数字人;
  回填的消息只入库, 不触发数字人、不标已读。没有核对位置的会话先把位置设成服务端当前位置;
- 每个会话从新往旧一批批拉, 拉到 N 天前、整批为空(加群前的拿不到)或到上限就停; 测试群、机器人推送这类
  名字的会话只拉 3 天、最多 200 条; 每批之后记断点, 中断后接着拉;
- 会话的更新时间按消息时间算, 回填完 larkx chats 的顺序还是真实的活跃顺序。
"""
import time
from datetime import datetime, timedelta

from loguru import logger

from . import groups, state

BATCH = 50
PER_CHAT = 2000
LOW_VALUE_DAYS, LOW_VALUE_CAP = 3, 200
TOTAL_CAP = 50000
PAUSE = 0.2
LOW_VALUE_WORDS = groups.TEST_WORDS + ('机器人', 'bot', 'robot', '通知', '助手', '推送')


class Backfill:
    def __init__(self, client, storage, owner_id: str, days: int, saved: dict = None, progress=None, sleep=time.sleep):
        self.client, self.storage, self.owner_id = client, storage, str(owner_id)
        self.days, self.sleep, self.progress = days, sleep, progress
        self.state = dict(saved or {})
        self.names = {}

    # 断点: {'since', 'order': [chat_id], 'pos': {chat_id: 下一批的最高位置, 0 = 拉完}, 'messages', 'chats_done', 'done'}

    def _save(self):
        if self.progress:
            self.progress(dict(self.state))

    def discover(self, only=None) -> list:
        """要回填的会话, 新的在前; 第一次调用时定下来并给没有核对位置的会话设上位置。"""
        if self.state.get('order') is not None and only is None:
            return self.state['order']
        since = time.time() - self.days * 86400
        ids = list(only) if only is not None else self.client.feed_chats(since, max_pages=50)
        info = {}
        for i in range(0, len(ids), 20):
            resp = self.client.api('chats.PullChatsByIdsRequest', {'chatIds': ids[i:i + 20]}) or {}
            info.update({str(k): v for k, v in (resp.get('chats') or {}).items()})
        # 只取 extra 里已有的核对位置(since 取将来, 不顺带登记本地别的会话)
        marks = self.storage.catchup_marks(datetime.now() + timedelta(days=1), extra=ids)
        pos, meta = {}, {}
        for cid in ids:
            ch = info.get(cid) or {}
            latest = int(ch.get('lastMessagePosition') or 0)
            if cid not in marks:
                self.storage.catchup_advance(cid, latest)
                marks[cid] = latest
            pos[cid] = marks[cid]
            meta[cid] = {'name': ch.get('name') or '', 'type': ch.get('type') if isinstance(ch.get('type'), int) else 0}
        self.state.update(since=since, order=ids, pos=pos, meta=meta, messages=self.state.get('messages', 0),
                          chats_done=0, done=False)
        self._save()
        return ids

    def _low_value(self, cid) -> bool:
        return groups._has((self.state.get('meta') or {}).get(cid, {}).get('name', ''), LOW_VALUE_WORDS)

    def _sender_name(self, uid, cid):
        key = (str(uid), str(cid))
        if key not in self.names:
            try:
                self.names[key] = self.client.get_user_name(uid, cid) or ''
            except Exception:
                self.names[key] = ''
        return self.names[key]

    def _store(self, cid, m) -> bool:
        if not m.get('from_id') or state.is_relay(m):
            return False
        meta = (self.state.get('meta') or {}).get(cid, {})
        own = str(m['from_id']) == self.owner_id
        sender = self._sender_name(m['from_id'], cid)
        chat_name = meta.get('name') or ('' if own else sender if m.get('chat_type') == 1 else '')
        try:
            return self.storage.save_message(m, sender_name=sender, chat_name=chat_name,
                                             direction='out' if own else 'in', at_msg_time=True)
        except Exception as e:
            logger.warning(f'回填入库失败 {cid}/{m.get("msg_id")}: {e!r}')
            return False

    def run(self, only=None, should_stop=None) -> dict:
        ids = self.discover(only)
        since = self.state['since']
        for n, cid in enumerate(ids):
            pos = self.state['pos'].get(cid, 0)
            low = self._low_value(cid)
            floor = time.time() - LOW_VALUE_DAYS * 86400 if low else since
            cap, got = (LOW_VALUE_CAP if low else PER_CHAT), 0
            while pos > 0 and got < cap and self.state['messages'] < TOTAL_CAP:
                if should_stop and should_stop():
                    self._save()
                    return self.result(stopped=True)
                lo = max(1, pos - BATCH + 1)
                msgs = self.client.pull_history(cid, range(lo, pos + 1))
                stored = sum(self._store(cid, m) for m in msgs if int(m.get('create_time') or 0) >= floor)
                got += stored
                self.state['messages'] += stored
                oldest = min([int(m.get('create_time') or 0) for m in msgs] or [0])
                pos = 0 if (not msgs or oldest < floor or lo == 1) else lo - 1
                self.state['pos'][cid] = pos
                self._save()
                self.sleep(PAUSE)
            self.state['pos'][cid] = 0
            self.state['chats_done'] = n + 1
            self._save()
            if self.state['messages'] >= TOTAL_CAP:
                logger.warning(f'回填达到总量上限 {TOTAL_CAP} 条, 剩下的会话不再回填')
                break
        if only is None:
            self.state['done'] = True
        self._save()
        return self.result()

    def result(self, stopped=False) -> dict:
        return {'chats': len(self.state.get('order') or []), 'chats_done': self.state.get('chats_done', 0),
                'messages': self.state.get('messages', 0), 'done': bool(self.state.get('done')), 'stopped': stopped}


def summary(res: dict) -> str:
    head = '回填完成' if res['done'] else ('回填暂停' if res.get('stopped') else '回填进行中')
    return f"{head}: {res['chats_done']}/{res['chats']} 个会话, 入库 {res['messages']} 条"
