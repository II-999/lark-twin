"""每日简报的数据: 都由代码从本地库算出来(确定的事实), 数字人据此写简报; 数字人没发出时直接发数据版兜底。

- 找你还没回的: 别人的私聊和 @本人 的群消息, 之后本人没在那个会话说过话; 只存不看的会话不算;
- 你在飞书里标记的消息(待处理);
- 重点关注的会话动态; 简单关注的会话里和本人有关的(@本人);
- 7 天内到期的提醒和任务; 例行任务的异常。
周一的简报看过去 7 天, 另加上周回顾。
"""
import time
from datetime import datetime, timedelta

from . import groups, jobs
from .storage.models import ChatProfile, Message, Task

FOCUS_LIMIT = 15
UNREPLIED_LIMIT = 20


def _fmt(ts) -> str:
    return datetime.fromtimestamp(int(ts)).strftime('%m-%d %H:%M') if ts else ''


def _flags(client, storage) -> list:
    """本人在飞书里标记的消息(只读地拉一次); 只列本地库里有的, 带上会话名和内容。"""
    if client is None:
        return []
    try:
        items = (client.api('feed.PullFlagsRequest', {'count': 50}) or {}).get('items') or []
    except Exception:
        return []
    ids = [str(i.get('itemId')) for i in items if i.get('itemId')]
    if not ids:
        return []
    s = storage.Session()
    try:
        rows = s.query(Message).filter(Message.msg_id.in_(ids)).all()
    finally:
        s.close()
    return [{'chat_id': r.chat_id, 'chat_name': r.chat_name, 'sender_name': r.sender_name, 'time': _fmt(r.create_time),
             'content': (r.content or '')[:120], 'msg_id': r.msg_id} for r in rows]


def briefing_data(storage, owner_id: str, now: datetime = None, client=None, home=None) -> dict:
    now = now or datetime.now()
    weekly = now.weekday() == 0
    hours = 24 * 7 if weekly else 24
    since = int(now.timestamp() - hours * 3600)
    cs = groups.ChatStore(storage)
    view = {r['chat_id']: r for r in cs.view()}
    owner_name = storage.owner_name(owner_id)
    unreplied = []
    for m in storage.mentions(owner_id, owner_name, since, limit=500):
        r = view.get(m['chat_id'], {})
        if m['replied'] or r.get('attention') == 'archive':
            continue
        unreplied.append({'chat_id': m['chat_id'], 'chat_name': m['chat_name'] or r.get('name', ''),
                          'sender_name': m['sender_name'], 'time': _fmt(m['create_time']),
                          'content': (m['content'] or '')[:120], 'bot': bool(r.get('peer_bot')),
                          'muted': bool(r.get('feishu_muted')), 'attention': r.get('attention', 'light')})
    unreplied = unreplied[-UNREPLIED_LIMIT:][::-1]
    act = groups.activity(storage, owner_id, since)
    focus = [r for r in act if r['attention'] == 'focus'][:FOCUS_LIMIT]
    light = [r for r in act if r['attention'] == 'light' and r['recent_at_me']]
    soon = now + timedelta(days=7)
    s = storage.Session()
    try:
        tasks = (s.query(Task).filter(Task.status == 'pending', Task.kind == 'task', Task.due_at <= int(soon.timestamp()))
                 .order_by(Task.due_at).limit(20).all())
    finally:
        s.close()
    store = jobs.JobStore(storage)
    broken = [jobs.describe(r) for r in store.log(limit=50)
              if r.status in ('failed', 'interrupted') and r.created_at and r.created_at >= now - timedelta(hours=hours)]
    # 新加入的会话: 窗口内第一次画像、还没确认的(回填时一次画进来的不算)
    bf_done = store.get_state('backfill').get('finished_at', 0)
    s = storage.Session()
    try:
        fresh = [p.chat_id for p in s.query(ChatProfile).filter(ChatProfile.confirmed == 0,
                 ChatProfile.created_at >= max(now - timedelta(hours=hours), datetime.fromtimestamp(bf_done + 600)))]
    finally:
        s.close()
    new_chats = [{'name': r['name'], 'group': r['suggested_group'], 'attention': r['attention_name'], 'reason': r['reason']}
                 for r in cs.view(fresh)]
    todo = []
    if home:
        from . import memory
        for e in memory.select(home, type_='事', status='待跟进', due_within=7, today=now.date()) + \
                [e for e in memory.select(home, type_='事', status='待跟进') if not e['meta'].get('due')]:
            m = e['meta']
            todo.append({'name': m.get('name') or e['file'], 'due': m.get('due', ''), 'people': m.get('people') or [],
                         'desc': m.get('desc', '')})
    week = {}
    if weekly:
        for r in act:
            week[r['attention_name']] = week.get(r['attention_name'], 0) + 1
    return {'date': now.strftime('%Y-%m-%d'), 'weekday': '一二三四五六日'[now.weekday()], 'weekly': weekly,
            'window': '过去 7 天' if weekly else '过去 24 小时',
            'unreplied': unreplied, 'flags': _flags(client, storage), 'focus': focus, 'light_related': light,
            'tasks': [{'id': t.id, 'due': _fmt(t.due_at), 'prompt': (t.prompt or '')[:100]} for t in tasks],
            'new_chats': new_chats, 'todo': todo,
            'broken_jobs': [{'job': b['job'], 'status': b['status_name'], 'error': b['error'][:100]} for b in broken],
            'week': week}


def render(data: dict) -> str:
    """数据整理成文字(给数字人参考, 也是兜底简报的正文)。"""
    lines = [f"## {data['date']} 周{data['weekday']} 工作简报" + ('(含上周回顾)' if data['weekly'] else '')]
    people = [u for u in data['unreplied'] if not u['bot']]
    notices = [u for u in data['unreplied'] if u['bot']]
    lines.append(f"\n**找你还没回的**({data['window']})")
    lines += [f"- {u['time']} 「{u['chat_name']}」{u['sender_name']}: {u['content']}" + (' 〔免打扰群〕' if u['muted'] else '')
              for u in people] or ['- 没有']
    if notices:
        lines.append('\n**需要你处理的通知**')
        lines += [f"- {u['time']} {u['chat_name']}: {u['content']}" for u in notices]
    if data['flags']:
        lines.append('\n**你标记的待处理**')
        lines += [f"- {f['time']} 「{f['chat_name']}」{f['sender_name']}: {f['content']}" for f in data['flags']]
    if data.get('todo'):
        lines.append('\n**待跟进的事**(记忆里的)')
        lines += [f"- {t['name']}" + (f"(截止 {t['due']})" if t['due'] else '') + (f": {t['desc']}" if t['desc'] else '')
                  + (f" — {'、'.join(t['people'])}" if t['people'] else '') for t in data['todo']]
    if data['tasks']:
        lines.append('\n**7 天内的提醒**')
        lines += [f"- {t['due']} {t['prompt']}" for t in data['tasks']]
    lines.append(f"\n**重点关注的会话**({data['window']})")
    lines += [f"- 「{r['name']}」{r['recent']} 条" + (f", 你说了 {r['recent_owner']} 条" if r['recent_owner'] else '')
              + (f", @你 {r['recent_at_me']} 次" if r['recent_at_me'] else '') for r in data['focus']] or ['- 没有动静']
    if data['light_related']:
        lines.append('\n**简单关注的会话里和你有关的**')
        lines += [f"- 「{r['name']}」@你 {r['recent_at_me']} 次" + (' 〔免打扰群〕' if r['feishu_muted'] else '')
                  for r in data['light_related']]
    if data.get('new_chats'):
        lines.append('\n**新加入的会话**(已按建议先归类, 要调整就回「/ai 把 xx 归到 yy 分类」)')
        lines += [f"- 「{c['name']}」→ {c['group'] or '未分类'} · {c['attention']}({c['reason']})" for c in data['new_chats']]
    if data['week']:
        lines.append('\n**上周概况**: ' + '、'.join(f'{k} {v} 个会话有消息' for k, v in data['week'].items()))
    if data['broken_jobs']:
        lines.append('\n**分身自己的问题**: ' + '; '.join(f"{b['job']} {b['status']}" for b in data['broken_jobs'])
                     + ' (larkx jobs log 查看)')
    return '\n'.join(lines)
