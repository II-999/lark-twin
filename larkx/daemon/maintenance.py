"""例行任务的调度和执行(见 larkx/jobs.py)。

daemon 的调度循环每 15 秒调一次 tick: 到期的任务先记运行记录再执行。
- local 任务在线程里跑本模块的 _job_<名字>, 不挡住调度循环;
- agent 任务排进收件箱作为一轮 system 维护任务, 这一轮结束时 daemon 回调 on_turn_done 记结果。
失败时给本人告警(同一任务 6 小时内只告一次), 详情在 larkx jobs log。
"""
import asyncio
import json
from datetime import datetime

from loguru import logger

from .. import backfill, digest, groups, jobs, retention
from .prompts import BUILDERS
from .triggers import build_report_prompt, build_system_prompt

ALERT_COOLDOWN = 6 * 3600
BOOTSTRAP_BATCH = 8


def bootstrap_chats(storage) -> list:
    """初始化记忆要看的会话: 重点关注(私人组除外), 30 天消息多的在前。"""
    rows = [r for r in groups.ChatStore(storage).view() if r['attention'] == 'focus' and not r['private']]
    return [r['chat_id'] for r in sorted(rows, key=lambda r: -(r['msgs'] or 0))]


class Maintenance:
    def __init__(self, daemon):
        self.d = daemon
        self.store = jobs.JobStore(daemon.storage)
        self._tasks = set()

    def recover(self, legacy_state: dict = None):
        n = self.store.recover()
        if n:
            logger.info(f'{n} 个上次没跑完的例行任务记为中断, 稍后重试')
        # 升级前每日回顾记在 daemon.json 里: 今天已经回顾过的补一条记录, 免得同一天再回顾一次
        today = datetime.now().strftime('%Y-%m-%d')
        if (legacy_state or {}).get('last_review') == today and not self.store.runs('review', today):
            self.store.create('review', today, trigger='startup', status='ok', summary='升级前今天已经回顾过')

    async def tick(self, now: datetime = None):
        now = now or datetime.now()
        for job in jobs.JOBS:
            if job.once:
                if job.when(self.d.cfg) is None:
                    continue
                key = self._once_slot(job)
                due = self.store.due(job, self.d.cfg, now, cur=(key, now)) if key else None
            else:
                due = self.store.due(job, self.d.cfg, now)
            if not due:
                continue
            if due[0] == 'skip':
                self.store.create(job.name, due[1], status='skipped', summary='错过计划时间, 这一次不跑')
                continue
            await self.start(job, due[1], 'schedule', due[2])

    async def start(self, job: jobs.Job, slot_key: str, trigger: str, attempt: int = 1):
        run = self.store.create(job.name, slot_key, trigger, attempt)
        logger.info(f'例行任务「{job.title}」开始({trigger}, #{run.id})')
        if job.kind == 'local':
            task = asyncio.create_task(self._run_local(job, run))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        else:
            from . import Event
            owner_chat = self.d.notifier.owner_chat()
            text = BUILDERS[job.name](self.d.cfg, self.d.storage, datetime.now(), str(self.d.auth.user_id),
                                      client=self.d.client, owner_chat=owner_chat)
            if job.kind == 'report':
                if not owner_chat:
                    self._failed(run, '找不到本人私聊, 发不了汇报')
                    return run
                ev = Event('report', build_report_prompt(job.name, text, owner_chat), chat_id=owner_chat, ref=run.ref,
                           run_id=run.id)
            else:
                ev = Event('system', build_system_prompt(text), chat_id=owner_chat, ref=run.ref, run_id=run.id)
            await self.d.inbox.put(ev)
        return run

    def _once_slot(self, job: jobs.Job):
        """一次性任务现在该不该跑, 该跑时返回时段名。"""
        if job.name == 'backfill':
            done = self.store.get_state('backfill').get('done')
            # 等启动后第一轮补收结束: 那一轮给最近的会话记好了核对位置, 回填只拉位置以下的
            return None if done or getattr(self.d, '_feed_since', None) is None else 'once'
        if job.name == 'bootstrap':
            bf, bs = self.store.get_state('backfill'), self.store.get_state('bootstrap')
            if not bf.get('done') or bs.get('done'):
                return None
            prof = self.store.last('profile', status=('ok',))
            if not prof or not prof.finished_at or prof.finished_at.timestamp() < bf.get('finished_at', 0):
                return None  # 回填后的画像还没跑完
            if 'chats' not in bs:
                bs['chats'] = bootstrap_chats(self.d.storage)
                bs['next'] = 0
                self.store.put_state('bootstrap', bs)
            if bs['next'] * BOOTSTRAP_BATCH >= len(bs['chats']):
                bs['done'] = True
                self.store.put_state('bootstrap', bs)
                return None
            return f"batch-{bs['next'] + 1}"
        if job.name == 'organize_init':
            bs = self.store.get_state('bootstrap')
            return 'once' if bs.get('done') else None  # 初始化记忆之后, 了解了本人的工作再提分组方案
        return None

    async def request(self, name: str) -> str:
        """手动立即运行(网页"立即运行"、larkx jobs run)。"""
        job = jobs.get(name)
        if not job:
            return f'没有例行任务 {name}, 可用: {", ".join(j.name for j in jobs.JOBS)}'
        run = await self.start(job, f'manual-{int(datetime.now().timestamp())}', 'manual')
        return f'已开始「{job.title}」#{run.id}'

    # ---- 结果 ----

    def started(self, run_id: int):
        self.store.start(run_id)

    def on_turn_done(self, ev, result):
        run = self.store.get(ev.run_id)
        if not run:
            return
        if run.job == 'briefing':
            return self._briefing_done(ev, run, result)
        if run.job in ('organize', 'organize_init') and result is not None and not result.is_error:
            return self._organize_done(run, result)
        if result is not None and not result.is_error:
            self.store.finish(run.id, result.text or '')
            if run.job == 'bootstrap' and run.slot.startswith('batch-'):
                bs = self.store.get_state('bootstrap')
                bs['next'] = max(bs.get('next', 0), int(run.slot[6:]))
                self.store.put_state('bootstrap', bs)
            return
        err = ('agent 会话中断, 结果未知' if result is None else f'{result.subtype}: {result.text or ""}')
        self._failed(run, err)

    def _organize_done(self, run, result):
        """整理会话这一轮结束: 把数字人提交的方案私信给本人; 没提交说明这次不需要调整。"""
        from ..organize import OrganizeStore
        store = OrganizeStore(self.d.storage)
        props = store.by_source(run.ref)
        for p in props:
            if self.d.notifier.send(store.render(p.id)):
                store.mark_notified(p.id)
        self.store.finish(run.id, (f"提交了方案 #{', #'.join(str(p.id) for p in props)}; " if props else '没有提交方案; ')
                          + (result.text or ''))

    def _briefing_done(self, ev, run, result):
        """简报这一轮结束: 查审计里有没有真的发到本人私聊; 没发出就由代码直接发数据版兜底。"""
        sent = any(a.action == 'send' and a.ok and a.chat_id == ev.chat_id
                   for a in self.d.storage.actions_for_turns([run.ref]))
        if sent and result is not None and not result.is_error:
            self.store.finish(run.id, result.text or '已发出')
            return
        data = digest.briefing_data(self.d.storage, str(self.d.auth.user_id), client=self.d.client)
        ok = self.d.notifier.send(digest.render(data) + '\n\n(分身这次没写成简报, 以上是数据版)')
        why = '数字人没有发出简报' if result is not None and not result.is_error else \
            ('agent 会话中断' if result is None else f'{result.subtype}: {result.text or ""}')
        self.store.fail(run.id, why + ('; 已发数据版兜底' if ok else '; 兜底也没发出去'))
        if not ok:
            self._failed(run, why + '; 兜底也没发出去')

    async def _run_local(self, job: jobs.Job, run):
        self.store.start(run.id)
        try:
            summary = await asyncio.to_thread(getattr(self, f'_job_{job.name}'), run)
            self.store.finish(run.id, summary if isinstance(summary, str) else json.dumps(summary, ensure_ascii=False))
            logger.info(f'例行任务「{job.title}」完成: {summary}')
            if job.name == 'backfill' and self.store.get_state('backfill').get('done'):
                await self.start(jobs.get('profile'), 'after-backfill', 'chain')  # 接着画像, 然后初始化记忆
        except Exception as e:
            logger.exception(f'例行任务「{job.title}」失败')
            self._failed(run, repr(e))

    # ---- 本地任务 ----

    def _job_profile(self, run) -> str:
        res = groups.profile_all(self.d.client, self.d.storage, str(self.d.auth.user_id))
        if res['new']:
            st = self.store.get_state('profile')
            st['new_chats'] = sorted(set(st.get('new_chats', [])) | set(res['new']))
            self.store.put_state('profile', st)
        return f"{res['chats']} 个会话; 新会话 {len(res['new'])} 个; 建议有变化 {res['changed']} 个"

    def _job_prune(self, run) -> str:
        st = self.store.get_state('prune')
        res = retention.run(self.d.cfg, self.d.storage, state=st)
        self.store.put_state('prune', st)
        return retention.summary(res)

    def _job_backfill(self, run) -> str:
        saved = self.store.get_state('backfill')

        def progress(st):
            if st.get('done') and not st.get('finished_at'):
                st['finished_at'] = datetime.now().timestamp()
            self.store.put_state('backfill', st)
        bf = backfill.Backfill(self.d.client, self.d.storage, str(self.d.auth.user_id),
                               int(self.d.cfg.get('backfill_days') or 30), saved=saved, progress=progress)
        res = bf.run()
        if not res['done']:
            raise RuntimeError(backfill.summary(res))
        return backfill.summary(res)

    def _failed(self, run, err: str):
        self.store.fail(run.id, err)
        job = jobs.get(run.job)
        title = job.title if job else run.job
        logger.error(f'例行任务「{title}」#{run.id} 失败: {err[:200]}')
        self.d.notifier.alert(f'例行任务「{title}」失败: {err[:150]}。详情: larkx jobs log --job {run.job}',
                              key=f'job:{run.job}', cooldown=ALERT_COOLDOWN)
