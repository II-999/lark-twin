"""结构化记忆: 字段解析、检查、自动索引、筛选、CLI; 开新会话前先整理记忆。"""
import argparse
import asyncio
import json
from datetime import date

import pytest

from larkx import cli, memory


def write(home, name, text):
    p = home / 'memory' / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding='utf-8')


@pytest.fixture
def home(tmp_path):
    write(tmp_path, '我.md', '---\ntype: 我\nname: 应灵昊\ndesc: 做防劣化\nupdated: 2026-11-03\n---\n负责发布前回归。')
    write(tmp_path, '人-陈威.md', '---\ntype: 人\nname: 陈威\nchats: [OC & 经验能力创编]\nupdated: 2026-11-01\n---\n同事。')
    write(tmp_path, '事-MR合入.md', '---\ntype: 事\nname: 防劣化 MR 合入\npeople: [陈威]\nstatus: 待跟进  # 等审核\n'
                                   'due: 2026-11-05\nupdated: 2026-11-03\n---\n四个仓库。')
    write(tmp_path, '事-旧事.md', '---\ntype: 事\nname: 旧事\nstatus: 已完成\nupdated: 2026-10-01\n---\n')
    write(tmp_path, '老笔记.md', '没有字段的旧文件')
    write(tmp_path, 'archive/群-老群.md', '---\ntype: 群\nname: 老群\nupdated: 2026-08-01\n---\n')
    return tmp_path


def test_parse():
    meta, body = memory.parse('---\ntype: 事\npeople: [陈威, 黄建磊]\nstatus: 待跟进 # 注释\n---\n正文')
    assert meta == {'type': '事', 'people': ['陈威', '黄建磊'], 'status': '待跟进'} and body == '正文'
    assert memory.parse('没有字段') == ({}, '没有字段')


def test_lint(home):
    write(home, '人-坏.md', '---\ntype: 同事\nname: 坏\nupdated: 11月3日\n---\n')
    issues = memory.lint(home, today=date(2026, 11, 3))
    got = {(l, f) for l, f, _ in issues}
    assert ('error', '人-坏.md') in got and ('warn', '老笔记.md') in got and ('warn', '事-旧事.md') in got
    assert not any(f.startswith('archive/') for _, f in got)
    assert not any(f in ('我.md', '人-陈威.md', '事-MR合入.md') for _, f in got)


def test_index_and_select(home):
    assert memory.build_index(home) and not memory.build_index(home)          # 第二次没变化
    idx = (home / 'memory' / 'MEMORY.md').read_text(encoding='utf-8')
    assert '## 我\n- [应灵昊](我.md) — 做防劣化' in idx
    assert '- [防劣化 MR 合入](事-MR合入.md) — 待跟进 截止 2026-11-05' in idx
    assert idx.index('防劣化 MR 合入') < idx.index('旧事')                    # 待跟进排在已完成前面
    assert '## 其他(还没有字段)\n- [老笔记](老笔记.md)' in idx and '老群' not in idx
    today = date(2026, 11, 3)
    assert [e['file'] for e in memory.select(home, type_='事', status='待跟进', due_within=7, today=today)] == ['事-MR合入.md']
    assert memory.select(home, type_='事', status='待跟进', due_within=1, today=today) == []
    assert {e['file'] for e in memory.select(home, about='陈威')} == {'人-陈威.md', '事-MR合入.md'}
    s = memory.stats(home)
    assert (s['files'], s['archived'], s['todo']) == (5, 1, 1)


def test_commit_rebuilds_index(home):
    from larkx.agent.home import commit_agent_home
    commit_agent_home(home, 'x')
    assert (home / 'memory' / 'MEMORY.md').exists()


def _args(**kw):
    return argparse.Namespace(**dict(dict(action='list', type=None, status=None, due=None, about=None, json=True), **kw))


def test_memory_cli(home, monkeypatch, capsys):
    from larkx.config import DEFAULTS
    monkeypatch.setattr(cli, 'load_config', lambda: dict(DEFAULTS, agent_home=str(home)))
    assert cli.cmd_memory(_args(type='事', status='待跟进')) == 0
    assert [r['name'] for r in json.loads(capsys.readouterr().out)] == ['防劣化 MR 合入']
    assert cli.cmd_memory(_args(action='lint')) == 0                          # 只有提醒, 没有错误
    capsys.readouterr()
    write(home, '人-坏.md', '---\ntype: 同事\n---\n')
    assert cli.cmd_memory(_args(action='lint')) == 1


def test_digest_includes_memory_todo(home, tmp_path):
    from larkx import digest
    from larkx.storage import Storage
    st = Storage(f'sqlite:///{tmp_path}/d.db')
    import datetime as dt
    d = digest.briefing_data(st, '100', now=dt.datetime(2026, 11, 3, 9), home=home)
    assert [t['name'] for t in d['todo']] == ['防劣化 MR 合入']
    assert '待跟进的事' in digest.render(d) and '截止 2026-11-05' in digest.render(d)


class ResetSession:
    def __init__(self):
        self.prompts, self.events, self.resets = [], [], 0

    async def ask(self, prompt):
        from larkx.agent import TurnResult
        self.prompts.append(prompt)
        return TurnResult('success', False, '', 0.1)

    async def interrupt(self):
        return False

    async def reset(self):
        self.resets += 1

    async def context_pressure(self):
        return None


def test_clear_flushes_memory_first(tmp_path, monkeypatch, fake_client):
    from tests.test_reliability import _daemon
    obj = _daemon(tmp_path, fake_client, monkeypatch)
    obj.session = ResetSession()
    obj._turns_since_flush = 2
    assert '先把这段对话里值得记的写进记忆' in asyncio.run(obj.control('clear'))
    ev = obj.inbox.get_nowait()
    assert ev.after == 'reset' and '开新会话' in ev.prompt and obj.session.resets == 0

    async def consume_one():
        obj.inbox.put_nowait(ev)
        task = asyncio.create_task(obj._consume())
        for _ in range(50):
            await asyncio.sleep(0.01)
            if obj.session.resets:
                break
        task.cancel()
    asyncio.run(consume_one())
    assert obj.session.resets == 1 and obj._turns_since_flush == 0
    assert any('已开启新会话' in t for c, t in fake_client.sent)
    assert '已开启新会话' in asyncio.run(obj.control('clear')) and obj.session.resets == 2   # 没聊过直接重置


def test_doctor_memory(home, tmp_path):
    from larkx import doctor
    assert doctor.check_memory({'agent_home': str(tmp_path / 'none')})['level'] == 'ok'
    c = doctor.check_memory({'agent_home': str(home)})
    assert c['level'] == 'ok' and '待跟进 1 件' in c['detail']
    write(home, '人-坏.md', '---\ntype: 同事\n---\n')
    assert doctor.check_memory({'agent_home': str(home)})['level'] == 'warn'
