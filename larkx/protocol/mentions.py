"""发消息时的真实 @: 正文里的 "@显示名" 就地换成 AT 元素, 正文里没写到的 @ 放到最前面。

mention 是 {'user_id', 'name'}(和文字消息的 at 片段一样)。正文里出现 "@显示名"、且显示名和某个 mention 的完全一致时,
那一处就是这个人的 @; 没出现的 mention 照旧放在正文最前面, 这样不会出现 "@张三 @张三" 重复。
- 显示名有包含关系时(张三 / 张三丰)先匹配长的。
- @ 前面紧挨着 ASCII 字母、数字或 ._%+- 时不算(是邮箱 a@b.com 这类); 显示名以 ASCII 字母数字结尾时,
  后面紧挨着 ASCII 字母数字或 _ 也不算(@bob 不匹配 @bobby)。中文名后面直接接正文("@张三你看下")照常匹配。
- markdown 富文本里, 只替换普通文字(TEXT, 包括加粗、斜体、列表、引用里的), 代码块和链接里的不动;
  替换出来的 AT 和拆开的文字保留原来那段文字的样式。和飞书客户端发的一样, AT 元素挂在段落里, id 记进 atIds。
- user_id 不是数字的 mention 退成普通文字 "@显示名"(和 builders.text_segments 的规则一致), 匿名发送的中转消息就靠这个
  把 @ 写成文字。
"""
import re

from . import richtext

AT = 5  # RichTextElement.Tag.AT


def _valid(m) -> bool:
    return str(m.get('user_id') or '').isdigit()


def _pattern(mentions):
    names = sorted({str(m.get('name') or '').lstrip('@') for m in mentions} - {''}, key=len, reverse=True)
    if not names:
        return None
    alts = [re.escape(n) + (r'(?![A-Za-z0-9_])' if re.search(r'[A-Za-z0-9]$', n) else '') for n in names]
    return re.compile(r'(?<![A-Za-z0-9._%+\-])@(' + '|'.join(alts) + ')')


def split_text(text: str, mentions) -> tuple:
    """把一段文字按 "@显示名" 切开: 返回 (片段, 用到的显示名集合), 片段是 ('text', 文字) 或 ('at', mention)。"""
    by_name = {}
    for m in mentions or ():
        by_name.setdefault(str(m.get('name') or '').lstrip('@'), m)
    pat = _pattern(mentions or ())
    if not text or pat is None:
        return ([('text', text)] if text else []), set()
    parts, used, pos = [], set(), 0
    for hit in pat.finditer(text):
        if hit.start() > pos:
            parts.append(('text', text[pos:hit.start()]))
        parts.append(('at', by_name[hit.group(1)]))
        used.add(hit.group(1))
        pos = hit.end()
    if pos < len(text):
        parts.append(('text', text[pos:]))
    return parts, used


def _unplaced(mentions, used):
    """正文里没写到的 mention(同一个显示名只放一次)。"""
    out, seen = [], set()
    for m in mentions or ():
        name = str(m.get('name') or '').lstrip('@')
        if name in used or name in seen:
            continue
        seen.add(name)
        out.append(m)
    return out


def text_segments(text: str, mentions) -> list:
    """纯文本消息的片段(builders 的 {'type': 'text'/'at'} 格式): 没写到的 @ 在最前面, 写到的就地替换。"""
    parts, used = split_text(text, mentions)
    segments = []
    for m in _unplaced(mentions, used):
        segments.extend([{'type': 'at', 'user_id': str(m.get('user_id') or ''), 'name': m['name']},
                         {'type': 'text', 'text': ' '}])
    for kind, v in parts:
        if kind == 'at':
            segments.append({'type': 'at', 'user_id': str(v.get('user_id') or ''), 'name': v['name']})
        else:
            segments.append({'type': 'text', 'text': v})
    return segments


def markdown_unplaced(markdown: str, mentions) -> list:
    """markdown 正文里没写到(会放到最前面)的 mention。"""
    if not mentions:
        return []
    from .proto.gateway import _lark_all
    return _unplaced(mentions, _build(_lark_all().entities.RichText(), markdown, list(mentions)))


def lead_text(mentions) -> str:
    """放在最前面的 @ 写成文字: "@甲 @乙 "。"""
    return ''.join('@' + m['name'] + ' ' for m in mentions or ())


def markdown_plain_text(markdown: str, mentions) -> str:
    """带 @ 的 markdown 消息的纯文本(审计、落库用): 就地的 @ 本来就是 "@显示名", 没写到的补在最前面。"""
    return (lead_text(markdown_unplaced(markdown, mentions)) + richtext.to_plain_text(markdown)).strip()


def build_rich_text(rich, markdown: str, mentions=()) -> str:
    """richtext.build_rich_text 再加上 @: 普通文字里的 "@显示名" 拆成 AT 元素, 没写到的放在第一段最前面。返回 innerText。"""
    _build(rich, markdown, list(mentions or ()))
    return rich.innerText


def _build(rich, markdown: str, mentions: list) -> set:
    """build_rich_text 的实现, 返回正文里就地替换掉的显示名。"""
    richtext.build_rich_text(rich, markdown)
    if not mentions:
        return set()
    from .proto.gateway import _lark_all
    E = _lark_all().entities.RichTextElement
    els = rich.elements
    d = els.dictionary
    counter = [len(d)]
    style_idx = {(s.name, s.value): i for i, s in enumerate(els.styles)}

    def new(tag, prop, style=None):
        counter[0] += 1
        eid = f'n{counter[0]}'   # 先用临时 id, 最后统一按文档顺序重新编号
        el = d[eid]
        el.tag = tag
        el.property = prop
        for name, value in (style or {}).items():
            el.style[name] = value
            if (name, value) not in style_idx:
                style_idx[(name, value)] = len(els.styles)
                els.styles.add(name=name, value=value)
            els.styleRefs[name].styleIds.append(style_idx[(name, value)])
        return eid

    def text_el(s, style=None):
        return new(richtext.TEXT, E.TextProperty(content=s).SerializeToString(), style)

    def at_el(m, style=None):
        if not _valid(m):
            return text_el('@' + m['name'], style)
        return new(AT, E.AtProperty(userId=str(m['user_id']), content='@' + m['name']).SerializeToString(), style)

    used = set()

    def walk(eid):
        el = d[eid]
        if not el.childIds:
            return
        out = []
        for cid in list(el.childIds):
            child = d[cid]
            if child.tag == richtext.TEXT:
                content = E.TextProperty.FromString(child.property).content
                parts, hit = split_text(content, mentions)
                if hit:
                    used.update(hit)
                    style = dict(child.style)
                    _drop(els, d, cid)
                    out.extend(at_el(v, style) if kind == 'at' else text_el(v, style) for kind, v in parts)
                    continue
            walk(cid)
            out.append(cid)
        del el.childIds[:]
        el.childIds.extend(out)

    root = rich.elementIds[0]
    walk(root)
    lead = _unplaced(mentions, used)
    if lead:
        blocks = d[root].childIds
        first = d[blocks[0]] if blocks else None
        nodes = []
        for m in lead:
            nodes.extend([at_el(m), text_el(' ')])
        if first is not None and first.tag == richtext.P:
            ids = nodes + list(first.childIds)
            del first.childIds[:]
            first.childIds.extend(ids)
            rich.innerText = ' '.join('@' + m['name'] for m in lead) + ' ' + rich.innerText
        else:
            _drop(els, d, nodes.pop())   # 单独成段时去掉最后的空格
            p = new(richtext.P, b'')
            d[p].childIds.extend(nodes)
            ids = [p] + list(blocks)
            del d[root].childIds[:]
            d[root].childIds.extend(ids)
            rich.innerText = (' '.join('@' + m['name'] for m in lead) + '\n' + rich.innerText).strip()
    _renumber(rich)
    del rich.atIds[:]
    rich.atIds.extend(k for k in _post_order(rich, rich.elementIds[0]) if d[k].tag == AT)
    return used


def _drop(els, d, eid):
    """删掉一个元素, 连同它在 styleRefs 里登记的样式引用。"""
    el = d[eid]
    for name, value in el.style.items():
        idx = next((i for i, s in enumerate(els.styles) if s.name == name and s.value == value), None)
        refs = els.styleRefs[name].styleIds
        if idx is not None and idx in refs:
            refs.remove(idx)
    del d[eid]


def _post_order(rich, root):
    d = rich.elements.dictionary
    out = []

    def go(eid):
        for c in d[eid].childIds:
            go(c)
        out.append(eid)
    go(root)
    return out


def _renumber(rich):
    """元素 id 按文档顺序(子元素在前、根最后)重新编成 1..n, 和 richtext._Tree 的编号方式一致。"""
    d = rich.elements.dictionary
    order = _post_order(rich, rich.elementIds[0])
    new_id = {old: str(i + 1) for i, old in enumerate(order)}
    copies = {}
    for old in order:
        el = type(d[old])()
        el.CopyFrom(d[old])
        children = [new_id[c] for c in el.childIds]
        del el.childIds[:]
        el.childIds.extend(children)
        copies[new_id[old]] = el
    d.clear()
    for k in sorted(copies, key=int):
        d[k].CopyFrom(copies[k])
    root = new_id[rich.elementIds[0]]
    del rich.elementIds[:]
    rich.elementIds.append(root)
