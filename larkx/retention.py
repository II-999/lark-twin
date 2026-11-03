"""清理: 按关注度删旧消息, 整理过期的运行记录。例行任务 prune 每天跑一次, 本人也可以用 larkx prune 手动跑。

- 重点关注的会话一直保留;
- 简单关注(含还没画像的会话)按 LARKX_RETENTION_LIGHT_DAYS, 默认一直保留;
- 只存不看按 LARKX_RETENTION_ARCHIVE_DAYS(默认 30 天)。按建议归为只存不看的, 画像建立满 7 天后才开始删,
  留时间给本人纠正; 本人确认过的马上按保留期算。
- 旧记录: 处理完的收件箱 30 天、办完的一次性任务 90 天、控制请求 7 天、例行任务运行记录 180 天。
  审计记录(actions)和监听任务的去重记录不删。
"""
import time
from datetime import datetime, timedelta

from . import groups
from .storage.models import ChatProfile, Control, InboxItem, JobRun, Task

DAY = 86400
PROTECT_DAYS = 7
HOUSEKEEP = (  # (说明, 表, 时间列, 保留天数, 额外条件)
    ('处理完的收件箱', InboxItem, InboxItem.created_at, 30, lambda: InboxItem.status != 'pending'),
    ('办完的一次性任务', Task, Task.created_at, 90,
     lambda: Task.status.in_(('done', 'failed', 'cancelled', 'unknown')) & (Task.every_sec == 0) & (Task.kind != 'event')),
    ('处理完的控制请求', Control, Control.created_at, 7, lambda: Control.status != 'pending'),
    ('例行任务运行记录', JobRun, JobRun.created_at, 180, lambda: JobRun.status.notin_(('queued', 'running'))),
)
VACUUM_ROWS = 10000
VACUUM_EVERY_DAYS = 7


def plan(cfg: dict, storage, now: float = None) -> list:
    """按关注度算出要删的消息: [(说明, 截止时间, chat_id 列表)]; 什么都不删时为空。"""
    now = time.time() if now is None else now
    s = storage.Session()
    try:
        profiles = {p.chat_id: p for p in s.query(ChatProfile).all()}
    finally:
        s.close()
    protect = datetime.fromtimestamp(now) - timedelta(days=PROTECT_DAYS)
    archive, light = [], []
    for r in groups.ChatStore(storage).view():
        p = profiles.get(r['chat_id'])
        if r['attention'] == 'archive':
            by_owner = r['attention_source'] in ('跟分类', '单独设置')
            if by_owner or (p and p.created_at and p.created_at <= protect):
                archive.append(r['chat_id'])
        elif r['attention'] == 'light':
            light.append(r['chat_id'])
    steps = []
    a_days, l_days = int(cfg.get('retention_archive_days') or 0), int(cfg.get('retention_light_days') or 0)
    if a_days > 0 and archive:
        steps.append((f'只存不看 {len(archive)} 个会话, 保留 {a_days} 天', now - a_days * DAY, archive))
    if l_days > 0 and light:
        steps.append((f'简单关注 {len(light)} 个会话, 保留 {l_days} 天', now - l_days * DAY, light))
    return steps


def housekeep(storage, now: float = None, dry_run: bool = False) -> list:
    """整理过期的运行记录, 返回 [(说明, 条数)]。"""
    now = datetime.fromtimestamp(time.time() if now is None else now)
    out = []
    s = storage.Session()
    try:
        for desc, model, col, days, cond in HOUSEKEEP:
            q = s.query(model).filter(col < now - timedelta(days=days), cond())
            out.append((desc, q.count() if dry_run else q.delete(synchronize_session=False)))
        if not dry_run:
            s.commit()
        return out
    finally:
        s.close()


def run(cfg: dict, storage, now: float = None, dry_run: bool = False, state: dict = None) -> dict:
    """按配置清理一次: 消息 + 旧记录; 删得多或很久没整理时整理一次数据库文件。state 记上次整理的时间。"""
    now = time.time() if now is None else now
    msgs = [(desc, storage.prune_messages(before, ids, dry_run=dry_run)) for desc, before, ids in plan(cfg, storage, now)]
    records = housekeep(storage, now, dry_run)
    total = sum(n for _, n in msgs) + sum(n for _, n in records)
    vacuumed = False
    if not dry_run and state is not None:
        last = state.get('last_vacuum', 0)
        if total >= VACUUM_ROWS or (total and now - last >= VACUUM_EVERY_DAYS * DAY):
            storage.vacuum()
            state['last_vacuum'], vacuumed = now, True
    return {'messages': msgs, 'records': records, 'total': total, 'vacuumed': vacuumed}


def summary(res: dict, dry_run: bool = False) -> str:
    verb = '会删' if dry_run else '删'
    parts = [f'{desc}: {verb} {n} 条' for desc, n in res['messages']] or ['消息: 没有要删的']
    rec = [f'{desc} {n}' for desc, n in res['records'] if n]
    parts.append(f'旧记录: {verb} ' + '、'.join(rec) if rec else '旧记录: 没有要删的')
    if res.get('vacuumed'):
        parts.append('已整理数据库文件')
    return '; '.join(parts)
