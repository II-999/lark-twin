"""结构化记忆: 字段解析、检查、自动索引、筛选、CLI; 默认关时提示词和 MEMORY.md 都不变。"""
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
    write(tmp_path, '我.md', '---\ntype: 我\nname: 王小明\ndesc: 做发布质量\nupdated: 2026-11-03\n---\n负责发布前回归。')
    write(tmp_path, '人-赵六.md', '---\ntype: 人\nname: 赵六\nchats: [项目群]\nupdated: 2026-11-01\n---\n同事。')
    write(tmp_path, '事-MR合入.md', '---\ntype: 事\nname: 接口 MR 合入\npeople: [赵六]\nstatus: 待跟进  # 等审核\n'
                                   'due: 2026-11-05\nupdated: 2026-11-03\n---\n四个仓库。')
    write(tmp_path, '事-旧事.md', '---\ntype: 事\nname: 旧事\nstatus: 已完成\nupdated: 2026-10-01\n---\n')
    write(tmp_path, '老笔记.md', '没有字段的旧文件')
    write(tmp_path, 'archive/群-老群.md', '---\ntype: 群\nname: 老群\nupdated: 2026-08-01\n---\n')
    return tmp_path


def test_parse():
    meta, body = memory.parse('---\ntype: 事\npeople: [赵六, 钱七]\nstatus: 待跟进 # 注释\n---\n正文')
    assert meta == {'type': '事', 'people': ['赵六', '钱七'], 'status': '待跟进'} and body == '正文'
    assert memory.parse('没有字段') == ({}, '没有字段')


def test_lint(home):
    write(home, '人-坏.md', '---\ntype: 同事\nname: 坏\nupdated: 11月3日\n---\n')
    issues = memory.lint(home, today=date(2026, 11, 3))
    got = {(l, f) for l, f, _ in issues}
    assert ('error', '人-坏.md') in got and ('warn', '老笔记.md') in got and ('warn', '事-旧事.md') in got
    assert not any(f.startswith('archive/') for _, f in got)
    assert not any(f in ('我.md', '人-赵六.md', '事-MR合入.md') for _, f in got)


def test_index_and_select(home):
    assert memory.build_index(home) and not memory.build_index(home)          # 第二次没变化
    idx = (home / 'memory' / 'MEMORY.md').read_text(encoding='utf-8')
    assert '## 我\n- [王小明](我.md) — 做发布质量' in idx
    assert '- [接口 MR 合入](事-MR合入.md) — 待跟进 截止 2026-11-05' in idx
    assert idx.index('接口 MR 合入') < idx.index('旧事')                    # 待跟进排在已完成前面
    assert '## 其他(还没有字段)\n- [老笔记](老笔记.md)' in idx and '老群' not in idx
    today = date(2026, 11, 3)
    assert [e['file'] for e in memory.select(home, type_='事', status='待跟进', due_within=7, today=today)] == ['事-MR合入.md']
    assert memory.select(home, type_='事', status='待跟进', due_within=1, today=today) == []
    assert {e['file'] for e in memory.select(home, about='赵六')} == {'人-赵六.md', '事-MR合入.md'}
    s = memory.stats(home)
    assert (s['files'], s['archived'], s['todo']) == (5, 1, 1)


def test_commit_rebuilds_index_only_when_on(home, monkeypatch):
    from larkx import config
    from larkx.agent.home import commit_agent_home
    (home / 'memory' / 'MEMORY.md').write_text('手写的索引', encoding='utf-8')
    commit_agent_home(home, 'x')
    assert (home / 'memory' / 'MEMORY.md').read_text(encoding='utf-8') == '手写的索引'          # 默认关: 不动
    monkeypatch.setattr(config, 'load_config', lambda: dict(config.DEFAULTS, memory_structured=True))
    commit_agent_home(home, 'y')
    assert '## 事' in (home / 'memory' / 'MEMORY.md').read_text(encoding='utf-8')


def _args(**kw):
    return argparse.Namespace(**dict(dict(action='list', type=None, status=None, due=None, about=None, json=True), **kw))


def test_memory_cli(home, monkeypatch, capsys):
    from larkx.config import DEFAULTS
    monkeypatch.setattr(cli, 'load_config', lambda: dict(DEFAULTS, agent_home=str(home)))
    assert cli.cmd_memory(_args(action='index')) == 1                         # 没打开结构化记忆, 不生成索引
    monkeypatch.setattr(cli, 'load_config', lambda: dict(DEFAULTS, agent_home=str(home), memory_structured=True))
    assert cli.cmd_memory(_args(action='index')) == 0
    capsys.readouterr()
    assert cli.cmd_memory(_args(type='事', status='待跟进')) == 0
    assert [r['name'] for r in json.loads(capsys.readouterr().out)] == ['接口 MR 合入']
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
    assert [t['name'] for t in d['todo']] == ['接口 MR 合入']
    assert '待跟进的事' in digest.render(d) and '截止 2026-11-05' in digest.render(d)


def test_prompts_unchanged_when_off():
    """默认关: 回顾、整理、上下文快满时的提示词和原来一字不差。"""
    from larkx.config import DEFAULTS
    from larkx.daemon import prompts
    cfg = dict(DEFAULTS)
    assert prompts.review(cfg, None, None) == (                                       # master 上的原文
        '每日回顾: 用 larkx chats 和 larkx messages 看一下最近 24 小时本人参与的会话(重点是本人发的消息和找本人的消息), '
        '把值得长期记住的新信息整理进 memory/ 并更新索引; 发现记忆里过时或错误的内容就改掉。'
        '答应过但还没安排的跟进, 用 larkx task add 补上。不要发飞书消息, 整理完直接结束。')
    assert prompts.consolidate(cfg, None, None) == prompts.CONSOLIDATE_PROMPT + prompts.MAINTENANCE_TAIL
    assert prompts.flush_prompt(cfg).endswith('更新 memory/MEMORY.md 索引。不要发飞书消息, 整理完直接结束。')
    on = dict(cfg, memory_structured=True)
    assert 'larkx memory lint' in prompts.review(on, None, None) and '并更新索引' not in prompts.review(on, None, None)
    assert 'larkx memory stats' in prompts.consolidate(on, None, None) and 'type: 我|人|群|事' in prompts.flush_prompt(on)


def test_doctor_memory(home, tmp_path):
    from larkx import doctor
    assert doctor.check_memory({'agent_home': str(home)}) is None                        # 没打开不检查
    on = {'agent_home': str(home), 'memory_structured': True}
    assert doctor.check_memory(dict(on, agent_home=str(tmp_path / 'none')))['level'] == 'ok'
    c = doctor.check_memory(on)
    assert c['level'] == 'ok' and '待跟进 1 件' in c['detail']
    write(home, '人-坏.md', '---\ntype: 同事\n---\n')
    assert doctor.check_memory(on)['level'] == 'warn'


def test_index_keeps_handwritten_descs(home):
    (home / 'memory' / 'MEMORY.md').write_text('# 索引\n\n- [陈述](人-赵六.md) — 后端负责人, 管审核\n'
                                                '- [老笔记](老笔记.md) — 以前的零散笔记\n', encoding='utf-8')
    memory.build_index(home)
    text = (home / 'memory' / 'MEMORY.md').read_text(encoding='utf-8')
    assert '[赵六](人-赵六.md) — 后端负责人, 管审核' in text and '[老笔记](老笔记.md) — 以前的零散笔记' in text
    assert 'desc: 后端负责人, 管审核' in (home / 'memory' / '人-赵六.md').read_text(encoding='utf-8')
    memory.build_index(home)
    assert (home / 'memory' / 'MEMORY.md').read_text(encoding='utf-8') == text          # 再生成不变、不叠加
