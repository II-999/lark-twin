"""markdown 富文本(post)构造的单元测试, 不访问网络。

服务端实测的约束(2026-10, 见 larkx/protocol/richtext.py 开头注释):
- type=2, 根是 DOCS(22); 容器元素也要显式带 property; innerText 必须显式设置, 否则 400。
- 行内代码 CODE(30)、标题会被服务端丢掉/降级, 所以发送侧就只保留文字。
"""
import pytest

from larkx.protocol import richtext
from larkx.protocol.proto import builders
from larkx.protocol.proto import lark_all_pb2 as LA

E = LA.entities.RichTextElement
TEXT, P, A, B, I, U, QUOTE, CODE_BLOCK = 1, 3, 6, 7, 8, 9, 29, 31
UL, OL, LI, DOCS = 26, 27, 28, 22


def _request(markdown):
    pkt = builders.build_message_packet({'kind': 'markdown', 'text': markdown}, 'c1', 'req')
    r = LA.messages.PutMessageRequest()
    r.ParseFromString(pkt.payload)
    return r


def _tree(r):
    """返回 {id: (tag, property_bytes, child_ids)} 和根 id 列表。"""
    d = r.content.richText.elements.dictionary
    return {k: (d[k].tag, d[k].property, list(d[k].childIds)) for k in d}


def _ids_by_tag(tree, tag):
    return [k for k, (t, _, _) in tree.items() if t == tag]


def test_markdown_is_post_with_docs_root():
    r = _request('你好')
    assert r.type == 2 and r.chatId == 'c1'
    tree = _tree(r)
    roots = list(r.content.richText.elementIds)
    assert len(roots) == 1 and tree[roots[0]][0] == DOCS
    # innerText 必须显式设置; 容器元素也要显式带 property
    assert r.content.richText.innerText == '你好'
    assert all(prop is not None for _, prop, _ in tree.values())


def test_simple_text_is_one_paragraph():
    r = _request('一行文字')
    tree = _tree(r)
    root = r.content.richText.elementIds[0]
    _, _, children = tree[root]
    assert len(children) == 1 and tree[children[0]][0] == P
    text_id = tree[children[0]][2][0]
    assert tree[text_id][0] == TEXT
    assert E.TextProperty.FromString(tree[text_id][1]).content == '一行文字'


def test_fenced_code_block_keeps_language_and_content():
    md = '先执行:\n\n```bash\nlarkx doctor\necho /ai\n```\n\n跑完贴结果'
    r = _request(md)
    tree = _tree(r)
    blocks = [n for n in tree.values() if n[0] == CODE_BLOCK]
    assert len(blocks) == 1
    cb = E.CodeBlockProperty.FromString(blocks[0][1])
    assert cb.language == 2  # BASH
    assert cb.content == 'larkx doctor\necho /ai\n'
    # 斜杠命令原样保留在纯文本里
    assert '/ai' in r.content.richText.innerText


def test_unknown_code_language_defaults_to_shell():
    r = _request('```brainfuck\nx\n```')
    tree = _tree(r)
    cb = E.CodeBlockProperty.FromString(next(p for t, p, _ in tree.values() if t == CODE_BLOCK))
    assert cb.language == 20  # SHELL


def test_ordered_and_unordered_lists():
    md = '- 甲\n- 乙\n\n1. 第一步\n2. 第二步'
    r = _request(md)
    tree = _tree(r)
    ul_ids, ol_ids = _ids_by_tag(tree, UL), _ids_by_tag(tree, OL)
    assert len(ul_ids) == 1 and len(ol_ids) == 1
    assert len(_ids_by_tag(tree, LI)) == 4
    assert E.ULProperty.FromString(tree[ul_ids[0]][1]).type == 0
    op = E.OLProperty.FromString(tree[ol_ids[0]][1])
    assert (op.type, op.start) == (0, 1)
    # 列表项挂在各自列表下, 两个列表是 DOCS 的直接子块
    assert set(tree[ul_ids[0]][2] + tree[ol_ids[0]][2]) == set(_ids_by_tag(tree, LI))
    root = r.content.richText.elementIds[0]
    assert set(ul_ids + ol_ids) <= set(tree[root][2])


def _styled_texts(r):
    """[(文字, style)]: 所有 TEXT 叶子, 按元素 id 顺序。"""
    d = r.content.richText.elements.dictionary
    return [(E.TextProperty.FromString(d[k].property).content, dict(d[k].style))
            for k in sorted(d, key=int) if d[k].tag == TEXT]


def test_bold_italic_are_styled_text_like_the_client():
    # 飞书客户端的加粗/斜体是带 style 的 TEXT; 老的 B(7)/I(8) 元素客户端显示成"当前版本不支持查看这部分内容"
    r = _request('**粗** 和 *斜* 还有 __粗2__ _斜2_')
    tree = _tree(r)
    assert not {B, I, U} & {t for t, _, _ in tree.values()}
    texts = _styled_texts(r)
    assert [s for s, st in texts if st == {'fontWeight': 'bold'}] == ['粗', '粗2']
    assert [s for s, st in texts if st == {'fontStyle': 'italic'}] == ['斜', '斜2']
    assert [st for s, st in texts if s in (' 和 ', ' 还有 ', ' ')] == [{}, {}, {}]
    assert r.content.richText.innerText == '粗 和 斜 还有 粗2 斜2'


def test_styles_are_registered_like_the_client():
    # 客户端发的消息: elements.styles 每种样式一条, styleRefs[样式名] 里每个带这项样式的元素记一次下标
    els = _request('**甲** *乙* **丙**').content.richText.elements
    assert [(s.name, s.value) for s in els.styles] == [('fontWeight', 'bold'), ('fontStyle', 'italic')]
    assert {k: list(v.styleIds) for k, v in els.styleRefs.items()} == {'fontWeight': [0, 0], 'fontStyle': [1]}
    plain = _request('没有样式').content.richText.elements
    assert not plain.styles and not plain.styleRefs


def test_underscores_inside_words_are_not_italic():
    md = '环境 ppe_kg_repo_kb，变量 callback_url 和 kb_version，_a_b_ 和 x _y_z'
    r = _request(md)
    assert _styled_texts(r) == [(md, {})]
    assert richtext.to_plain_text(md) == md
    # 两边不挨着字母数字时照常是斜体/加粗, 中文标点旁边也算
    texts = _styled_texts(_request('（_斜_）__粗__，'))
    assert ('斜', {'fontStyle': 'italic'}) in texts and ('粗', {'fontWeight': 'bold'}) in texts


def test_asterisks_with_inner_spaces_are_not_italic():
    assert _styled_texts(_request('2 * 3 * 4')) == [('2 * 3 * 4', {})]
    assert _styled_texts(_request('a ** b ** c')) == [('a ** b ** c', {})]


def test_inline_code_degrades_to_plain_text():
    r = _request('运行 `larkx send` 就行')
    tree = _tree(r)
    assert 30 not in {t for t, _, _ in tree.values()}   # 不发 CODE 元素
    assert 'larkx send' in r.content.richText.innerText


def test_bare_and_markdown_links():
    # A(6) 的属性是 AnchorProperty: 裸链接 href/content/textContent 都是 url(同客户端), [文字](url) 是自定义文字的链接
    r = _request('看 https://a.com 和 [飞书](https://b.com)')
    tree = _tree(r)
    links = [E.AnchorProperty.FromString(tree[k][1]) for k in sorted(tree, key=int) if tree[k][0] == A]
    assert [(x.href, x.content, x.textContent, x.isCustom, x.scene) for x in links] == [
        ('https://a.com', 'https://a.com', 'https://a.com', False, 0),
        ('https://b.com', '飞书', '', True, 3),
    ]
    assert r.content.richText.innerText == '看 https://a.com 和 飞书'
    # 审计用的纯文本里文字和 url 都留着
    assert richtext.to_plain_text('[飞书](https://b.com)') == '飞书 https://b.com'


def test_quote_block():
    r = _request('> 引用一句\n> 第二行')
    tree = _tree(r)
    quotes = [n for n in tree.values() if n[0] == QUOTE]
    assert len(quotes) == 1
    # 引用里的内容是段落
    assert all(tree[c][0] == P for c in quotes[0][2])


def test_heading_degrades_to_paragraph_text():
    r = _request('# 标题\n\n正文')
    tree = _tree(r)
    assert not any(t in (23, 24, 25, 44, 45, 46) for t, _, _ in tree.values())
    assert '标题' in r.content.richText.innerText


def test_element_ids_are_sequential_in_document_order():
    r = _request('```bash\nx\n```\n\n- 甲\n')
    ids = sorted(r.content.richText.elements.dictionary.keys(), key=int)
    assert ids == [str(i + 1) for i in range(len(ids))]


def test_plain_text_for_audit_and_prefix_guard():
    md = '# 标题\n\n**重点** `/ai`\n\n- 一\n- 二\n\n```bash\nls\n```'
    plain = richtext.to_plain_text(md)
    assert plain.startswith('标题')
    assert '/ai' in plain and '重点' in plain
    assert '- 一' in plain and 'ls' in plain
    assert '**' not in plain and '`' not in plain and '#' not in plain
    assert builders.plain_text({'kind': 'markdown', 'text': md}) == plain


def test_empty_markdown_does_not_crash():
    r = _request('')
    assert r.type == 2
    assert r.content.richText.innerText == ''


# ---- 接收侧: 富文本树 -> 纯文本摘要 ----

def _summary(markdown):
    from protobuf_to_dict import protobuf_to_dict
    from larkx.protocol.proto import decoders
    r = _request(markdown)
    d = protobuf_to_dict(r.content)
    return decoders.extract_rich_text(d.get('richText') or {})


def test_summary_has_code_block_lists_and_inline_text():
    s = _summary('开头:\n\n```bash\nlarkx doctor\n```\n\n- 甲\n- 乙\n\n1. 一\n2. 二')
    assert 'larkx doctor' in s
    assert '- 甲' in s and '- 乙' in s
    assert '1. 一' in s and '2. 二' in s
    assert '开头' in s


def test_summary_keeps_bold_italic_and_link_url():
    s = _summary('**粗** *斜* 看 https://a.com')
    assert '粗' in s and '斜' in s and 'https://a.com' in s


def test_summary_flat_layout_still_works():
    # 老的扁平式 type=4 消息: 叶子直接挂在 elementIds 下
    pkt = builders.build_send_message_packet('第一行\n第二行', 'c1', 'req')
    r = LA.messages.PutMessageRequest()
    r.ParseFromString(pkt.payload)
    from protobuf_to_dict import protobuf_to_dict
    from larkx.protocol.proto import decoders
    d = protobuf_to_dict(r.content)
    assert decoders.extract_rich_text(d.get('richText') or {}) == '第一行\n第二行'


# ---- 裸链接边界(GFM autolink 规则) ----

def _links(r):
    """[(href, content)] 构造结果里所有 A 叶子, 按文档顺序。"""
    d = r.content.richText.elements.dictionary
    out = []
    for k in sorted(d, key=int):
        if d[k].tag == A:
            p = E.AnchorProperty.FromString(d[k].property)
            out.append((p.href, p.content))
    return out


def _text_leaves(r):
    """[(文字, style)] 所有 TEXT 叶子, 按文档顺序。"""
    d = r.content.richText.elements.dictionary
    out = []
    for k in sorted(d, key=int):
        if d[k].tag == TEXT:
            out.append((E.TextProperty.FromString(d[k].property).content, dict(d[k].style)))
    return out


@pytest.mark.parametrize('text,href,after', [
    ('详情见 http://a.com。还有', 'http://a.com', '。还有'),
    ('链接 http://a.com，继续', 'http://a.com', '，继续'),
    ('是这个吗 http://a.com？对', 'http://a.com', '？对'),
    ('加油 http://a.com！冲', 'http://a.com', '！冲'),
    ('甲 http://a.com、乙', 'http://a.com', '、乙'),
    ('见 http://a.com：如下', 'http://a.com', '：如下'),
    ('注 http://a.com；另', 'http://a.com', '；另'),
    ('省略号 http://a.com…完', 'http://a.com', '…完'),
    ('ASCII 句读 http://a.com,.!?;:.', 'http://a.com', ',.!?;:.'),
    ('结尾点 http://a.com.', 'http://a.com', '.'),
    ('两个点 http://a.com/..', 'http://a.com/', '..'),
])
def test_bare_url_drops_trailing_cjk_and_ascii_punctuation(text, href, after):
    r = _request(text)
    assert _links(r) == [(href, href)]
    assert any(s == after and not st for s, st in _text_leaves(r)), _text_leaves(r)


def test_bare_url_balances_parentheses_like_gfm():
    # 结尾多余的右括号不进 href, 但 URL 里成对的括号保留
    assert _links(_request('(见 http://a.com)')) == [('http://a.com', 'http://a.com')]
    leaves = _text_leaves(_request('(见 http://a.com)'))
    assert [s for s, _ in leaves] == ['(见 ', ')']
    assert _links(_request('见 http://a.com/Foo_(bar) 吧')) == [
        ('http://a.com/Foo_(bar)', 'http://a.com/Foo_(bar)')]
    # query 里成对括号, 结尾多一个 )
    assert _links(_request('x http://a.com/?a=(1)&b=2) y')) == [
        ('http://a.com/?a=(1)&b=2', 'http://a.com/?a=(1)&b=2')]
    # 全角括号在 URL 外, 不进链接
    r = _request('链接（http://a.com）')
    assert _links(r) == [('http://a.com', 'http://a.com')]
    assert [s for s, _ in _text_leaves(r)] == ['链接（', '）']


def test_bare_url_keeps_punctuation_in_middle():
    # 句读标点在 URL 中间(query/path)是合法部分, 不能剥
    r = _request('见 http://a.com/p,q;x?a=1&b=2!z 结束')
    assert _links(r) == [('http://a.com/p,q;x?a=1&b=2!z', 'http://a.com/p,q;x?a=1&b=2!z')]
    # 尾斜杠保留
    assert _links(_request('见 http://a.com/')) == [('http://a.com/', 'http://a.com/')]
    # 结尾引号不进 href
    r = _request('他说 http://a.com”然后')
    assert _links(r) == [('http://a.com', 'http://a.com')]


def test_markdown_link_url_allows_balanced_inner_parens():
    # markdown 链接分支: 最后一个 ) 是语法分隔, URL 内部允许成对括号(维基百科这类链接)
    r = _request('[wiki](https://en.wikipedia.org/wiki/Foo_(bar)) 后')
    assert _links(r) == [('https://en.wikipedia.org/wiki/Foo_(bar)', 'wiki')]
    r = _request('[x](http://a.com/?a=(b)&c=(d))')
    assert _links(r) == [('http://a.com/?a=(b)&c=(d)', 'x')]


def test_uppercase_scheme_is_recognized_and_href_keeps_original_case():
    # 问题 4: 协议名大小写不敏感, href 保持原文大小写
    r = _request('看 HTTPS://EXAMPLE.COM/X 吧')
    assert _links(r) == [('HTTPS://EXAMPLE.COM/X', 'HTTPS://EXAMPLE.COM/X')]
    r = _request('[t](HTTPS://EXAMPLE.COM/x)')
    assert _links(r) == [('HTTPS://EXAMPLE.COM/x', 't')]
    r = _request('Http://Example.com/foo')
    assert _links(r) == [('Http://Example.com/foo', 'Http://Example.com/foo')]


# ---- 加粗/斜体内部递归 ----

def test_bold_link_is_one_anchor_with_bold_style():
    # **[文档](url)** -> 一个内容为"文档"的 A 叶子, 且带粗体样式(飞书客户端自己也这么发)
    r = _request('**[文档](https://b.com)**')
    d = r.content.richText.elements.dictionary
    a = [d[k] for k in sorted(d, key=int) if d[k].tag == A]
    assert len(a) == 1
    p = E.AnchorProperty.FromString(a[0].property)
    assert (p.href, p.content, p.isCustom, p.scene) == ('https://b.com', '文档', True, 3)
    assert dict(a[0].style) == {'fontWeight': 'bold'}
    # styles/styleRefs 登记和普通粗体文字一致
    els = r.content.richText.elements
    assert [(s.name, s.value) for s in els.styles] == [('fontWeight', 'bold')]
    assert {k: list(v.styleIds) for k, v in els.styleRefs.items()} == {'fontWeight': [0]}
    assert r.content.richText.innerText == '文档'
    # 前后还有文字时各归各的叶子
    r = _request('前 **[文档](https://b.com)** 后')
    d = r.content.richText.elements.dictionary
    a = [d[k] for k in d if d[k].tag == A]
    assert len(a) == 1 and dict(a[0].style) == {'fontWeight': 'bold'}
    assert [s for s, st in _text_leaves(r) if not st] == ['前 ', ' 后']


def test_anchor_style_survives_protobuf_roundtrip():
    # 结构层面的单元测试: A 元素带 style 构造、序列化、再解析
    prop = E.AnchorProperty(href='https://b.com', content='文档', isCustom=True, scene=3).SerializeToString()
    el = E(tag=A, property=prop)
    el.style['fontWeight'] = 'bold'
    raw = el.SerializeToString()
    el2 = E.FromString(raw)
    assert el2.tag == A
    assert dict(el2.style) == {'fontWeight': 'bold'}
    p = E.AnchorProperty.FromString(el2.property)
    assert (p.href, p.content, p.isCustom, p.scene) == ('https://b.com', '文档', True, 3)


def test_italic_bold_nest_either_way():
    # *a **b** c*: 斜体里套加粗, 样式叠加
    leaves = _text_leaves(_request('*a **b** c*'))
    assert leaves == [
        ('a ', {'fontStyle': 'italic'}),
        ('b', {'fontStyle': 'italic', 'fontWeight': 'bold'}),
        (' c', {'fontStyle': 'italic'}),
    ]
    # **a *b* c**: 加粗里套斜体
    leaves = _text_leaves(_request('**a *b* c**'))
    assert leaves == [
        ('a ', {'fontWeight': 'bold'}),
        ('b', {'fontWeight': 'bold', 'fontStyle': 'italic'}),
        (' c', {'fontWeight': 'bold'}),
    ]
    # 斜体里的裸链接也带斜体样式
    r = _request('*看 https://a.com*')
    d = r.content.richText.elements.dictionary
    a = [d[k] for k in d if d[k].tag == A]
    assert len(a) == 1 and dict(a[0].style) == {'fontStyle': 'italic'}


def test_bold_italic_triple_markers():
    # ***x*** 粗斜体叠加
    assert _text_leaves(_request('***x***')) == [
        ('x', {'fontWeight': 'bold', 'fontStyle': 'italic'})]
    # 夹在普通文字里
    assert _text_leaves(_request('a ***x*** b')) == [
        ('a ', {}),
        ('x', {'fontWeight': 'bold', 'fontStyle': 'italic'}),
        (' b', {}),
    ]


def test_recursive_inline_does_not_loop_and_plain_text_strips_markers():
    # **a** 内部的 a 不再匹配 bold
    assert _text_leaves(_request('**a**')) == [('a', {'fontWeight': 'bold'})]
    assert richtext.to_plain_text('**[文档](https://b.com)**') == '文档 https://b.com'
    assert richtext.to_plain_text('*a **b** c*') == 'a b c'
    assert richtext.to_plain_text('***x***') == 'x'


def test_bold_url_in_summary():
    s = _summary('**[飞书](https://b.com)** 看 https://a.com')
    assert '飞书(https://b.com)' in s and 'https://a.com' in s


# ---- 接收侧: AnchorProperty / 历史 LinkProperty ----

def _decode_a(prop_bytes):
    from larkx.protocol.proto import decoders
    rich = {'elements': {'dictionary': {'1': {'tag': A, 'property': prop_bytes}}},
            'elementIds': ['1']}
    return decoders.extract_rich_text(rich)


def test_decode_custom_anchor_keeps_text_and_href():
    prop = E.AnchorProperty(href='https://b.com', content='飞书', isCustom=True, scene=3).SerializeToString()
    assert _decode_a(prop) == '飞书(https://b.com)'


def test_decode_bare_anchor_outputs_url_only():
    prop = E.AnchorProperty(
        href='https://a.com', content='https://a.com', textContent='https://a.com').SerializeToString()
    assert _decode_a(prop) == 'https://a.com'


def test_decode_legacy_link_property_outputs_url_without_ios_url():
    # 历史消息是 LinkProperty(url, iosUrl): Anchor 解时 field2 会误读成 content,
    # 但没有 isCustom/scene 标记, 只能输出 url, iosUrl 不能泄漏成自定义文字
    prop = E.LinkProperty(
        url='https://old.example.com/x', iosUrl='https://ios.example.com/y').SerializeToString()
    assert _decode_a(prop) == 'https://old.example.com/x'
    # 只填 url 的更老消息
    prop = E.LinkProperty(url='https://old.example.com/2').SerializeToString()
    assert _decode_a(prop) == 'https://old.example.com/2'


def test_decode_a_does_not_crash_on_bad_property():
    assert _decode_a(b'') == ''
    assert _decode_a(b'\xff\xff\xff') == ''


# ---- 复查补的回归用例 ----

@pytest.mark.parametrize('url', [
    'https://kiro.dev/docs/skills',      # i/k/s: 整条正则开 re.I 时会在这里被截断
    'HTTPS://KIRO.DEV/SKILLS',
    'http://isk.example.com/Kelvin?s=1',
])
def test_bare_url_with_i_k_s_is_not_truncated(url):
    r = _request(f'看 {url} 吧')
    assert _links(r) == [(url, url)]
    assert [s for s, _ in _text_leaves(r)] == ['看 ', ' 吧']


def test_markdown_link_with_i_k_s_and_parens():
    r = _request('[k](https://kiro.dev/s(i)k)')
    assert _links(r) == [('https://kiro.dev/s(i)k', 'k')]


@pytest.mark.parametrize('url', [
    'https://a.com/?sig=ab==',   # 签名结尾的 = 属于 URL
    'https://a.com/#',
    'https://a.com/a-b-',
    'https://a.com/a+',
    'https://a.com/?q=x&',
])
def test_bare_url_keeps_non_gfm_trailing_chars(url):
    assert _links(_request(f'见 {url} 吧')) == [(url, url)]


def test_bare_url_balances_square_and_curly_brackets():
    r = _request('[看 http://a.com]')
    assert _links(r) == [('http://a.com', 'http://a.com')]
    assert [s for s, _ in _text_leaves(r)] == ['[看 ', ']']
    assert _links(_request('{http://a.com}')) == [('http://a.com', 'http://a.com')]
    assert _links(_request('见 http://a.com/[x] 吧')) == [('http://a.com/[x]', 'http://a.com/[x]')]


def test_bold_match_starting_with_three_stars_does_not_lose_text():
    # "***x** y*" 由加粗分支匹配到 "***x**", 不能当成粗斜体切掉两头三个字符
    leaves = _text_leaves(_request('***x** y*'))
    assert ''.join(s for s, _ in leaves) == '*x y*'
    leaves = _text_leaves(_request('***x* y**'))
    assert leaves == [('x', {'fontWeight': 'bold', 'fontStyle': 'italic'}), (' y', {'fontWeight': 'bold'})]
    assert richtext.to_plain_text('***x* y**') == 'x y'


def test_decode_legacy_link_property_with_enum_fields():
    # LinkProperty 的 5(specialUrl, enum)、6(scene, enum) 和 AnchorProperty 的 5/6(string)类型不同,
    # 按 Anchor 解析不能报错, 也不能把 iosUrl 当成自定义文字
    prop = E.LinkProperty(url='https://old.example.com/x', iosUrl='https://ios.example.com/y',
                          specialUrl=1, scene=1).SerializeToString()
    assert _decode_a(prop) == 'https://old.example.com/x'
