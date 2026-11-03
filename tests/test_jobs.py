"""例行任务: 时段计算、只跑一次、失败重试、重启中断、运行记录、告警、手动运行、CLI。"""
import argparse
import asyncio
import json
from datetime import datetime, timedelta

import pytest

from larkx import cli, jobs
from larkx.config import DEFAULTS
from larkx.storage import Storage

REVIEW = jobs.get('review')
CONSOLIDATE = jobs.get('consolidate')
MON_5AM = datetime(2026, 11, 2, 5, 0)   # 周一


@pytest.fixture
def store(tmp_path):
    return jobs.JobStore(Storage(f'sqlite:///{tmp_path}/j.db'))


def cfg(**kw):
    return dict(json.loads(json.dumps(DEFAULTS)), **kw)


def test_slots():
    c = cfg(memory_review_hour=4, memory_consolidate_weekday=0)
    assert jobs.slot(REVIEW, c, MON_5AM) == ('2026-11-02', datetime(2026, 11, 2, 4, 0))
    assert jobs.slot(REVIEW, c, datetime(2026, 11, 2, 3, 59)) is None          # 还没到点
    assert jobs.slot(CONSOLIDATE, c, MON_5AM)[0] == '2026-11-02'
    assert jobs.slot(CONSOLIDATE, c, MON_5AM + timedelta(days=1)) is None      # 周二不整理
    assert jobs.slot(REVIEW, cfg(memory_review_hour=-1), MON_5AM) is None       # 关掉
    assert jobs.next_time(CONSOLIDATE, c, MON_5AM) == datetime(2026, 11, 9, 4, 0)
    assert jobs.next_time(REVIEW, c, MON_5AM) == datetime(2026, 11, 3, 4, 0)


def test_due_once_per_slot_and_retry(store):
    c, at = cfg(), (lambda m: MON_5AM + timedelta(minutes=m))
    assert store.due(REVIEW, c, at(0)) == ('run', '2026-11-02', 1)
    run = store.create('review', '2026-11-02')
    assert store.due(REVIEW, c, at(0)) is None                   # 排着队
    store.fail(run.id, 'boom')
    store._set(run.id, finished_at=at(0))
    assert store.due(REVIEW, c, at(10)) is None                  # 刚失败, 30 分钟后才重试
    assert store.due(REVIEW, c, at(31)) == ('run', '2026-11-02', 2)
    r2 = store.create('review', '2026-11-02', attempt=2)
    store.fail(r2.id, 'boom again')
    store._set(r2.id, finished_at=at(31))
    assert store.due(REVIEW, c, at(120)) is None                 # 最多两次
    store.create('review', '2026-11-03', status='ok')
    assert store.due(REVIEW, c, MON_5AM + timedelta(days=1)) is None   # 第二天已完成


def test_grace_skips_late_runs(store):
    late = jobs.Job('late', '迟到不跑', 'local', lambda c: (9, 0, None), grace=3 * 3600)
    assert store.due(late, cfg(), datetime(2026, 11, 2, 11, 0)) == ('run', '2026-11-02', 1)
    assert store.due(late, cfg(), datetime(2026, 11, 2, 12, 30)) == ('skip', '2026-11-02')


def test_recover_marks_interrupted(store):
    a = store.create('review', 's1')
    b = store.create('consolidate', 's1')
    store.start(b.id)
    store.create('review', 's0', status='ok')
    assert store.recover() == 2
    assert {store.get(a.id).status, store.get(b.id).status} == {'interrupted'}


def _daemon(tmp_path, monkeypatch, fake_client):
    from tests.test_reliability import _daemon as make
    obj = make(tmp_path, fake_client, monkeypatch)
    obj.cfg.update(memory_review_hour=0, memory_consolidate_weekday=-1)
    return obj


def test_agent_job_runs_through_inbox(tmp_path, monkeypatch, fake_client):
    obj = _daemon(tmp_path, monkeypatch, fake_client)
    asyncio.run(obj.maintenance.tick())
    asyncio.run(obj.maintenance.tick())                           # 同一时段不会重复排
    assert obj.inbox.qsize() == 1
    ev = obj.inbox.get_nowait()
    run = obj.maintenance.store.get(ev.run_id)
    assert (ev.kind, ev.chat_id, ev.ref, run.status) == ('system', 'self', f'job:review#{run.id}', 'queued')
    assert ev.prompt.startswith('<system>每日回顾')
    asyncio.run(obj._handle(ev))
    run = obj.maintenance.store.get(ev.run_id)
    assert run.status == 'ok' and run.started_at and run.finished_at


def test_agent_job_failure_alerts_once(tmp_path, monkeypatch, fake_client):
    from larkx.agent import TurnResult
    obj = _daemon(tmp_path, monkeypatch, fake_client)
    asyncio.run(obj.maintenance.tick())
    ev = obj.inbox.get_nowait()
    obj.maintenance.on_turn_done(ev, TurnResult('error_max_turns', True, '超时了', 1.0))
    assert obj.maintenance.store.get(ev.run_id).status == 'failed'
    alerts = [t for c, t in fake_client.sent if '例行任务「每日回顾」失败' in t]
    assert len(alerts) == 1 and 'larkx jobs log --job review' in alerts[0]
    run2 = obj.maintenance.store.create('review', 'x')
    obj.maintenance.on_turn_done(type('E', (), {'run_id': run2.id})(), None)
    assert len([t for c, t in fake_client.sent if '例行任务「每日回顾」失败' in t]) == 1   # 6 小时内只告一次


def test_local_job_success_and_failure(tmp_path, monkeypatch, fake_client):
    from larkx.daemon.maintenance import Maintenance
    obj = _daemon(tmp_path, monkeypatch, fake_client)
    ok = jobs.Job('demo_ok', '演示', 'local', lambda c: (0, 0, None))
    bad = jobs.Job('demo_bad', '会失败', 'local', lambda c: (0, 0, None))
    monkeypatch.setattr(jobs, 'JOBS', [ok, bad])
    monkeypatch.setattr(Maintenance, '_job_demo_ok', lambda self, run: '删掉 3 条', raising=False)

    def boom(self, run):
        raise RuntimeError('网络不通')
    monkeypatch.setattr(Maintenance, '_job_demo_bad', boom, raising=False)

    async def run():
        await obj.maintenance.tick()
        await asyncio.gather(*list(obj.maintenance._tasks))
    asyncio.run(run())
    store = obj.maintenance.store
    assert store.last('demo_ok').status == 'ok' and store.last('demo_ok').summary == '删掉 3 条'
    assert store.last('demo_bad').status == 'failed' and '网络不通' in store.last('demo_bad').error


def test_manual_run_via_control(tmp_path, monkeypatch, fake_client):
    obj = _daemon(tmp_path, monkeypatch, fake_client)
    obj.cfg['memory_review_hour'] = 23 if datetime.now().hour < 23 else -1   # 还没到点, 只能手动
    text = asyncio.run(obj.control('job:review'))
    assert text.startswith('已开始「每日回顾」#')
    ev = obj.inbox.get_nowait()
    assert obj.maintenance.store.get(ev.run_id).trigger == 'manual'
    assert '没有例行任务' in asyncio.run(obj.control('job:nope'))


def test_legacy_daemon_state_counts_as_reviewed(tmp_path, monkeypatch, fake_client):
    obj = _daemon(tmp_path, monkeypatch, fake_client)
    obj.maintenance.recover({'last_review': datetime.now().strftime('%Y-%m-%d')})
    asyncio.run(obj.maintenance.tick())
    assert obj.inbox.qsize() == 0


def _jobs_args(**kw):
    return argparse.Namespace(**dict(dict(action='list', name=None, job=None, limit=20, preview=False, json=True), **kw))


def test_jobs_cli(tmp_path, monkeypatch, capsys):
    st = Storage(f'sqlite:///{tmp_path}/c.db')
    monkeypatch.setattr(cli, 'Storage', lambda: st)
    store = jobs.JobStore(st)
    r = store.create('review', '2026-11-02')
    store.finish(r.id, '记了 3 条')
    assert cli.cmd_jobs(_jobs_args()) == 0
    rows = {x['name']: x for x in json.loads(capsys.readouterr().out)}
    assert rows['review']['last']['summary'] == '记了 3 条' and rows['review']['enabled']
    assert cli.cmd_jobs(_jobs_args(action='log')) == 0
    assert json.loads(capsys.readouterr().out)[0]['status'] == 'ok'
    assert cli.cmd_jobs(_jobs_args(action='run', name='review', preview=True, json=False)) == 0
    assert '每日回顾' in capsys.readouterr().out
    monkeypatch.setattr(cli, 'inside_twin', lambda: True)
    assert cli.cmd_jobs(_jobs_args(action='run', name='review')) == 3


def test_hooks_block_jobs_run():
    from larkx.agent.hooks import check_tool_use
    assert check_tool_use('Bash', {'command': 'larkx jobs run review'})
    assert check_tool_use('Bash', {'command': 'larkx jobs log --job review'}) is None


def test_doctor_jobs(tmp_path):
    from larkx import doctor
    st = Storage(f'sqlite:///{tmp_path}/d.db')
    assert doctor.check_jobs(cfg(), st)['level'] == 'ok'
    store = jobs.JobStore(st)
    store.fail(store.create('review', 's').id, 'x')
    assert doctor.check_jobs(cfg(), st)['level'] == 'warn'
