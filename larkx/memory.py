"""长期记忆的结构和工具: 记忆仍是 memory/ 下的 Markdown 文件(一个主题一个文件), 开头加几行固定字段:

    ---
    type: 事              # 我 | 人 | 群 | 事
    name: 防劣化 MR 合入
    desc: 四个仓库的 MR, 等陈威审核    # 索引里的一句话说明(可选)
    people: [陈威]
    chats: [OC & 经验能力创编]
    status: 待跟进        # 只用于 事: 进行中 | 待跟进 | 已完成
    due: 2026-11-07       # 只用于 事, 可空
    updated: 2026-11-03   # 最后确认的日期, 必填
    ---
    正文自由写。

memory/MEMORY.md 是索引, 由代码按这些字段生成(按 我/人/群/事 分组), 不用手写; 旧内容放 memory/archive/。
lint 检查字段、过期和体积, list 按字段筛选, CLI、网页、doctor、简报都用这里。
"""
import re
from datetime import date, datetime, timedelta
from pathlib import Path

TYPES = ('我', '人', '群', '事')
STATUSES = ('进行中', '待跟进', '已完成')
LIST_FIELDS = ('people', 'chats', 'aliases')
INDEX = 'MEMORY.md'
INDEX_LIMIT = 60
STALE_DAYS = 60
DONE_ARCHIVE_DAYS = 7
MAX_BYTES = 8000
INDEX_HEAD = '# 记忆索引\n\n由代码根据各文件开头的字段生成, 不要手改; 改记忆请改对应文件。\n'


def parse(text: str):
    """拆出开头 --- 之间的字段和正文; 没有字段时返回 ({}, 全文)。只支持 key: value 和 key: [a, b]。"""
    m = re.match(r'\A---\s*\n(.*?)\n---\s*(?:\n|\Z)', text, re.S)
    if not m:
        return {}, text
    meta = {}
    for line in m.group(1).splitlines():
        line = re.sub(r'\s+#.*$', '', line).rstrip()
        if not line.strip() or ':' not in line:
            continue
        key, _, val = line.partition(':')
        key, val = key.strip(), val.strip().strip('"\'')
        if val.startswith('[') and val.endswith(']'):
            val = [v.strip().strip('"\'') for v in val[1:-1].split(',') if v.strip()]
        meta[key] = val
    return meta, text[m.end():]


def _date(text):
    try:
        return datetime.strptime(str(text).strip(), '%Y-%m-%d').date()
    except ValueError:
        return None


def entries(home: Path, archived: bool = False) -> list:
    """记忆文件列表: [{file, meta, body, size}]; archived 时只看 archive/。"""
    base = Path(home) / 'memory' / ('archive' if archived else '')
    out = []
    for p in sorted(base.glob('*.md')) if base.is_dir() else []:
        if p.name == INDEX and not archived:
            continue
        text = p.read_text(encoding='utf-8', errors='replace')
        meta, body = parse(text)
        out.append({'file': (f'archive/{p.name}' if archived else p.name), 'meta': meta, 'body': body,
                    'size': len(text.encode('utf-8'))})
    return out


def lint(home: Path, today: date = None) -> list:
    """[(级别 error|warn|info, 文件, 说明)]。"""
    today = today or date.today()
    out = []
    for e in entries(home):
        m, f = e['meta'], e['file']
        if not m:
            out.append(('warn', f, '没有开头的字段(type/name/updated), 索引里只能按文件名列'))
            continue
        for k in ('type', 'name', 'updated'):
            if not m.get(k):
                out.append(('error', f, f'缺少 {k}'))
        if m.get('type') and m['type'] not in TYPES:
            out.append(('error', f, f'type 只能是 {"/".join(TYPES)}, 不是 {m["type"]!r}'))
        if m.get('status') and m['status'] not in STATUSES:
            out.append(('error', f, f'status 只能是 {"/".join(STATUSES)}, 不是 {m["status"]!r}'))
        for k in ('updated', 'due'):
            if m.get(k) and not _date(m[k]):
                out.append(('error', f, f'{k} 要写成 YYYY-MM-DD, 不是 {m[k]!r}'))
        upd = _date(m.get('updated') or '')
        if upd and m.get('type') != '我' and today - upd > timedelta(days=STALE_DAYS):
            out.append(('warn', f, f'{STALE_DAYS} 天没再确认, 不再相关就移到 archive/'))
        if m.get('status') == '已完成' and upd and today - upd > timedelta(days=DONE_ARCHIVE_DAYS):
            out.append(('warn', f, '已完成超过 7 天, 移到 archive/ 或删掉'))
        if e['size'] > MAX_BYTES:
            out.append(('warn', f, f'{e["size"] // 1000}KB 太大, 拆开或精简'))
    n = len(entries(home))
    if n > INDEX_LIMIT:
        out.append(('warn', INDEX, f'{n} 个记忆文件, 超过 {INDEX_LIMIT} 个, 合并或归档一些'))
    return out


def _index_line(e) -> str:
    m = e['meta']
    desc = m.get('desc') or ''
    if m.get('type') == '事':
        bits = [b for b in (m.get('status'), f"截止 {m['due']}" if m.get('due') else '') if b]
        desc = '; '.join([x for x in (desc, ' '.join(bits)) if x])
    return f"- [{m.get('name') or e['file'][:-3]}]({e['file']})" + (f' — {desc}' if desc else '')


def build_index(home: Path) -> bool:
    """按字段重写 memory/MEMORY.md; 内容有变化时返回 True。"""
    path = Path(home) / 'memory' / INDEX
    if not path.parent.is_dir():
        return False
    by_type = {t: [] for t in TYPES}
    other = []
    for e in entries(home):
        (by_type.get(e['meta'].get('type')) if e['meta'].get('type') in TYPES else other).append(e)
    parts = [INDEX_HEAD]
    for t, rows in list(by_type.items()) + [('其他(还没有字段)', other)]:
        if rows:
            if t == '事':
                order = {s: i for i, s in enumerate(('待跟进', '进行中', '', '已完成'))}
                rows.sort(key=lambda e: (order.get(e['meta'].get('status', ''), 2), e['meta'].get('due') or '9999'))
            parts.append(f'\n## {t}\n' + '\n'.join(_index_line(e) for e in rows))
    text = '\n'.join(parts).rstrip() + '\n'
    if path.exists() and path.read_text(encoding='utf-8') == text:
        return False
    path.write_text(text, encoding='utf-8')
    return True


def select(home: Path, type_: str = None, status: str = None, due_within: int = None, about: str = None,
           today: date = None) -> list:
    """按字段筛选: 类型、状态、截止日期在 N 天内(含已过期)、和某个人或群有关(名字包含)。"""
    today = today or date.today()
    out = []
    for e in entries(home):
        m = e['meta']
        if type_ and m.get('type') != type_:
            continue
        if status and m.get('status') != status:
            continue
        if due_within is not None:
            d = _date(m.get('due') or '')
            if not d or d > today + timedelta(days=due_within):
                continue
        if about:
            hay = [m.get('name', '')] + [x for k in LIST_FIELDS for x in (m.get(k) or [])]
            if not any(about in str(h) for h in hay):
                continue
        out.append(e)
    return out


def stats(home: Path) -> dict:
    cur, arch = entries(home), entries(home, archived=True)
    by_type = {}
    for e in cur:
        t = e['meta'].get('type') or '无字段'
        by_type[t] = by_type.get(t, 0) + 1
    issues = lint(home)
    return {'files': len(cur), 'archived': len(arch), 'by_type': by_type,
            'todo': len(select(home, type_='事', status='待跟进')),
            'bytes': sum(e['size'] for e in cur),
            'errors': sum(1 for i in issues if i[0] == 'error'), 'warnings': sum(1 for i in issues if i[0] == 'warn')}
