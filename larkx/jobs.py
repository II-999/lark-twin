"""例行任务: 每日回顾、每周整理记忆等, 由 daemon 按时段调度, CLI、网页、doctor 都看得到运行记录。

每个任务在一个时段(通常是一天)里跑一次: 到点后先建一条运行记录(queued)再执行, 记录先于执行,
不会出现"记了跑过其实没跑"。失败或被中断(daemon 重启)的时段过一会儿重试, 最多 max_attempts 次;
只看当前时段, 停机错过的昨天不补跑。
"""
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Optional

from sqlalchemy import desc

from .storage.models import JobRun, JobState

ACTIVE = ('queued', 'running')
SETTLED = ('ok', 'skipped')
STATUS_NAMES = {'queued': '排队', 'running': '运行中', 'ok': '完成', 'failed': '失败', 'skipped': '跳过',
                'interrupted': '中断'}


@dataclass(frozen=True)
class Job:
    name: str
    title: str
    kind: str        # local: daemon 里直接跑的代码; agent: 交给数字人的一轮(维护任务)
    when: Callable   # cfg -> (时, 分, 星期几或 None); 关掉时返回 None
    grace: int = 0   # 到点后超过这么多秒还没开始就不跑了, 记为跳过(0 = 当天内都补)
    max_attempts: int = 2
    retry_after: int = 1800


def _review_at(cfg):
    hour = cfg.get('memory_review_hour', -1)
    return (hour, 0, None) if 0 <= hour <= 23 else None


def _consolidate_at(cfg):
    hour, weekday = cfg.get('memory_review_hour', -1), cfg.get('memory_consolidate_weekday', -1)
    return (hour if 0 <= hour <= 23 else 4, 0, weekday) if 0 <= weekday <= 6 else None


def _before_review(cfg):
    """回顾前半小时: 先刷新会话画像, 回顾时用上最新的关注度。关掉回顾时按 3:30。只在打开会话分类时跑。"""
    if not cfg.get('chat_groups'):
        return None
    hour = cfg.get('memory_review_hour', -1)
    return ((hour - 1) % 24 if 0 <= hour <= 23 else 3, 30, None)


def _after_review(cfg):
    """回顾后半小时清理; 关掉回顾时按 4:30。只在配了保留天数时跑(默认都不删, 手动 larkx prune 随时可用)。"""
    if not (int(cfg.get('retention_archive_days') or 0) > 0 or int(cfg.get('retention_light_days') or 0) > 0):
        return None
    hour = cfg.get('memory_review_hour', -1)
    return (hour if 0 <= hour <= 23 else 4, 30, None)


JOBS = [
    Job('profile', '会话画像', 'local', _before_review),
    Job('review', '每日回顾', 'agent', _review_at),
    Job('consolidate', '每周整理记忆', 'agent', _consolidate_at),
    Job('prune', '清理旧数据', 'local', _after_review),
]


def get(name: str) -> Optional[Job]:
    return next((j for j in JOBS if j.name == name), None)


def slot(job: Job, cfg: dict, now: datetime):
    """当前时段的 (时段名, 计划时间); 今天不该跑或还没到点时返回 None。"""
    when = job.when(cfg)
    if when is None:
        return None
    hour, minute, weekday = when
    if weekday is not None and now.weekday() != weekday:
        return None
    due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return (due.strftime('%Y-%m-%d'), due) if now >= due else None


def next_time(job: Job, cfg: dict, now: datetime) -> Optional[datetime]:
    """下一次计划时间(只用来展示)。"""
    when = job.when(cfg)
    if when is None:
        return None
    hour, minute, weekday = when
    for days in range(8):
        t = (now + timedelta(days=days)).replace(hour=hour, minute=minute, second=0, microsecond=0)
        if t > now and (weekday is None or t.weekday() == weekday):
            return t
    return None


class JobStore:
    def __init__(self, storage):
        self.Session = storage.Session

    def create(self, job: str, slot_key: str, trigger: str = 'schedule', attempt: int = 1, status: str = 'queued',
               summary: str = '') -> JobRun:
        s = self.Session()
        try:
            run = JobRun(job=job, slot=slot_key, trigger=trigger, attempt=attempt, status=status, summary=summary)
            if status in SETTLED:
                run.finished_at = datetime.now()
            s.add(run)
            s.flush()
            run.ref = f'job:{job}#{run.id}'
            s.commit()
            return run
        finally:
            s.close()

    def _set(self, run_id: int, **fields):
        s = self.Session()
        try:
            run = s.get(JobRun, run_id)
            if not run:
                return None
            for k, v in fields.items():
                setattr(run, k, v)
            s.commit()
            return run
        finally:
            s.close()

    def start(self, run_id: int):
        return self._set(run_id, status='running', started_at=datetime.now())

    def finish(self, run_id: int, summary: str = ''):
        return self._set(run_id, status='ok', finished_at=datetime.now(), summary=(summary or '')[:4000])

    def fail(self, run_id: int, error: str):
        return self._set(run_id, status='failed', finished_at=datetime.now(), error=(error or '')[:4000])

    def get(self, run_id: int):
        s = self.Session()
        try:
            return s.get(JobRun, run_id)
        finally:
            s.close()

    def runs(self, job: str, slot_key: str) -> list:
        s = self.Session()
        try:
            return s.query(JobRun).filter(JobRun.job == job, JobRun.slot == slot_key).order_by(JobRun.id).all()
        finally:
            s.close()

    def last(self, job: str, status=None):
        s = self.Session()
        try:
            q = s.query(JobRun).filter(JobRun.job == job)
            if status:
                q = q.filter(JobRun.status.in_(status))
            return q.order_by(desc(JobRun.id)).first()
        finally:
            s.close()

    def log(self, job: str = None, limit: int = 20) -> list:
        s = self.Session()
        try:
            q = s.query(JobRun)
            if job:
                q = q.filter(JobRun.job == job)
            return q.order_by(desc(JobRun.id)).limit(limit).all()
        finally:
            s.close()

    def recover(self) -> int:
        """daemon 重启: 上次排着队或正在跑的记为中断, 之后按重试规则重新跑。"""
        s = self.Session()
        try:
            rows = s.query(JobRun).filter(JobRun.status.in_(ACTIVE)).all()
            for r in rows:
                r.status, r.finished_at, r.error = 'interrupted', datetime.now(), '服务重启时还没跑完'
            s.commit()
            return len(rows)
        finally:
            s.close()

    def due(self, job: Job, cfg: dict, now: datetime):
        """该不该跑: None 不跑; ('skip', 时段) 错过时间; ('run', 时段, 第几次)。"""
        cur = slot(job, cfg, now)
        if not cur:
            return None
        key, planned = cur
        runs = self.runs(job.name, key)
        if any(r.status in ACTIVE + SETTLED for r in runs):
            return None
        if not runs and job.grace and (now - planned).total_seconds() > job.grace:
            return ('skip', key)
        if len(runs) >= job.max_attempts:
            return None
        if runs and (now - (runs[-1].finished_at or runs[-1].created_at)).total_seconds() < job.retry_after:
            return None
        return ('run', key, len(runs) + 1)

    def get_state(self, job: str) -> dict:
        s = self.Session()
        try:
            row = s.get(JobState, job)
            return json.loads(row.data) if row and row.data else {}
        finally:
            s.close()

    def put_state(self, job: str, data: dict):
        s = self.Session()
        try:
            row = s.get(JobState, job) or JobState(job=job)
            row.data = json.dumps(data, ensure_ascii=False)
            s.merge(row)
            s.commit()
        finally:
            s.close()


def describe(run) -> dict:
    """运行记录给 CLI、网页展示用的字段。"""
    if not run:
        return {}
    fmt = (lambda t: t.strftime('%Y-%m-%d %H:%M:%S') if t else '')
    return {'id': run.id, 'job': run.job, 'slot': run.slot, 'attempt': run.attempt, 'trigger': run.trigger,
            'status': run.status, 'status_name': STATUS_NAMES.get(run.status, run.status), 'ref': run.ref,
            'created_at': fmt(run.created_at), 'started_at': fmt(run.started_at), 'finished_at': fmt(run.finished_at),
            'summary': run.summary or '', 'error': run.error or ''}
