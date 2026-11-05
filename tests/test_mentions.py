"""--mention 的位置: 正文里的 "@显示名" 就地变成 @, 没写到的放最前面; 纯文本和 markdown 富文本两种。不访问网络。"""
import argparse
import json

from larkx.protocol import mentions
from larkx.protocol.proto import builders
from larkx.protocol.proto import lark_all_pb2 as LA
from larkx.storage import Storage

E = LA.entities.RichTextElement
TEXT, AT, P, LI, QUOTE, CODE_BLOCK, DOCS = 1, 5, 3, 28, 29, 31, 22
MOU = {'type': 'at', 'user_id': '7518957269125398529', 'name': '牟站通'}
ZS = {'type': 'at', 'user_id': '200', 'name': '张三'}


def _req(msg):
    r = LA.messages.PutMessageRequest()
    r.ParseFromString(builders.build_message_packet(msg, 'c1', 'req').payload)
    return r


def _flat(r):
    """按文档顺序展开叶子: (tag, 内容或 userId, 样式)。"""
    rich = r.content.richText
    d = rich.elements.dictionary
    out = []

    def go(k):
        el = d[k]
        if el.childIds:
            for c in el.childIds:
                go(c)
        elif el.tag == AT:
            out.append((AT, E.AtProperty.FromString(el.property).userId, dict(el.style)))
        elif el.tag == TEXT:
            out.append((TEXT, E.TextProperty.FromString(el.property).content, dict(el.style)))
        else:
            out.append((el.tag, '', {}))
    for k in rich.elementIds:
        go(k)
    return out


def test_text_mentions_in_place_and_leading():
    segs = mentions.text_segments('@牟站通 你看下, 抄送 @张三丰', [MOU, ZS])
    # 两个都写在正文里: 都就地(中文名后面不设边界, "@张三丰" 里的 "@张三" 也算写到)
    assert segs[0] == {'type': 'at', 'user_id': '7518957269125398529', 'name': '牟站通'}
    assert builders.plain_text({'kind': 'text', 'segments': segs}) == '@牟站通 你看下, 抄送 @张三丰'
    segs = mentions.text_segments('你看下', [MOU, ZS])
    assert builders.plain_text({'kind': 'text', 'segments': segs}) == '@牟站通 @张三 你看下'
    assert [s['type'] for s in builders.text_segments({'segments': segs})] == ['at', 'text', 'at', 'text']


def test_text_mention_no_duplicate_and_boundaries():
    bob = {'user_id': '9', 'name': 'bob'}
    segs = mentions.text_segments('mail bob@x.com, @bobby 不是, @bob 是', [bob])
    assert [s for s in segs if s['type'] == 'at'] == [{'type': 'at', 'user_id': '9', 'name': 'bob'}]
    assert builders.plain_text({'kind': 'text', 'segments': segs}) == 'mail bob@x.com, @bobby 不是, @bob 是'
    # 显示名有包含关系时先匹配长的
    a, b = {'user_id': '1', 'name': '张三'}, {'user_id': '2', 'name': '张三丰'}
    segs = mentions.text_segments('@张三丰 和 @张三', [a, b])
    assert [s['user_id'] for s in segs if s['type'] == 'at'] == ['2', '1']
    r = _req({'kind': 'text', 'segments': segs})
    assert sorted(r.atUserIdList) == [1, 2] and r.content.richText.innerText == '@张三丰 和 @张三'


def test_markdown_mentions_in_place_inside_bold_list_quote_not_code():
    md = '**@牟站通** 看下\n\n- 列表 @牟站通\n\n> 引用 *@牟站通*\n\n```\n@牟站通 代码\n```'
    r = _req({'kind': 'markdown', 'text': md, 'mentions': [MOU]})
    rich = r.content.richText
    assert r.type == 2 and list(r.atUserIdList) == [7518957269125398529]
    flat = _flat(r)
    ats = [x for x in flat if x[0] == AT]
    assert [x[2] for x in ats] == [{'fontWeight': 'bold'}, {}, {'fontStyle': 'italic'}]
    # 没有额外放到最前面的 @, 代码块里的 @ 原样保留
    assert flat[0][0] == AT and flat[1] == (TEXT, ' 看下', {})
    code = [k for k in rich.elements.dictionary if rich.elements.dictionary[k].tag == CODE_BLOCK]
    assert '@牟站通 代码' in E.CodeBlockProperty.FromString(rich.elements.dictionary[code[0]].property).content
    assert len(rich.atIds) == 3 and all(rich.elements.dictionary[k].tag == AT for k in rich.atIds)
    assert '@' + '牟站通' in rich.innerText and rich.innerText.count('@牟站通') == 4
    # id 仍按文档顺序编号: 子在前、根最后, 1..n 连续
    d = rich.elements.dictionary
    assert sorted(d, key=int) == [str(i) for i in range(1, len(d) + 1)]
    assert rich.elementIds == [str(len(d))] and d[str(len(d))].tag == DOCS
    for k in d:
        assert all(int(c) < int(k) for c in d[k].childIds)
    # 样式登记: 每个带样式的元素在 styleRefs 里记一次
    refs = {name: len(v.styleIds) for name, v in rich.elements.styleRefs.items()}
    styled = {}
    for k in d:
        for name in d[k].style:
            styled[name] = styled.get(name, 0) + 1
    assert refs == styled


def test_markdown_leading_mentions():
    r = _req({'kind': 'markdown', 'text': '**结论**: 可以', 'mentions': [MOU, ZS]})
    flat = _flat(r)
    assert flat[:4] == [(AT, '7518957269125398529', {}), (TEXT, ' ', {}), (AT, '200', {}), (TEXT, ' ', {})]
    assert r.content.richText.innerText == '@牟站通 @张三 结论: 可以'
    assert builders.plain_text({'kind': 'markdown', 'text': '**结论**: 可以', 'mentions': [MOU, ZS]}) == '@牟站通 @张三 结论: 可以'
    # 第一块不是段落(列表、代码块)时, @ 单独成一段放在最前面
    r = _req({'kind': 'markdown', 'text': '- a\n- b', 'mentions': [MOU]})
    d = r.content.richText.elements.dictionary
    first = d[d[r.content.richText.elementIds[0]].childIds[0]]
    assert first.tag == P and [d[c].tag for c in first.childIds] == [AT]
    assert r.content.richText.innerText.startswith('@牟站通\n')
    # 只在代码块里写到的不算写到
    r = _req({'kind': 'markdown', 'text': '```\n@牟站通\n```', 'mentions': [MOU]})
    assert _flat(r)[0] == (AT, '7518957269125398529', {})


def test_markdown_without_mentions_unchanged_and_relay_writes_text():
    plain = _req({'kind': 'markdown', 'text': '**a** b'})
    same = _req({'kind': 'markdown', 'text': '**a** b', 'mentions': []})
    assert plain.content.richText == same.content.richText
    msg = {'kind': 'markdown', 'text': '@牟站通 看下', 'mentions': [MOU, ZS]}
    assert builders.has_mentions(msg)
    relay = builders.without_mentions(msg)
    assert not builders.has_mentions(relay)
    r = _req(relay)
    assert list(r.atUserIdList) == [] and list(r.content.richText.atIds) == []
    assert r.content.richText.innerText == '@张三 @牟站通 看下'


def _send(monkeypatch, fake_client, text, mention, markdown=False):
    from larkx import cli
    monkeypatch.setattr(cli, 'LarkAuth', lambda: object())
    monkeypatch.setattr(cli, 'LarkClient', lambda _: fake_client)
    sent = []
    fake_client.send_message = lambda msg, *a, **kw: sent.append(msg) or 'sent'
    args = argparse.Namespace(chat_id='target', text=[text], root=None, json=True, mention=mention, markdown=markdown)
    assert cli.cmd_send(args) == 0
    return sent[0], Storage().get_messages('target')[-1]


def test_cli_text_mention_in_place(monkeypatch, fake_client):
    msg, row = _send(monkeypatch, fake_client, '@牟站通 看下', ['7518957269125398529:牟站通'])
    assert msg['segments'][0] == MOU and builders.plain_text(msg) == '@牟站通 看下'
    assert row.content.startswith('@牟站通 看下') and not row.content.startswith('@牟站通 @牟站通')
    assert json.loads(row.content_data)['_larkx_mention_ids'] == ['7518957269125398529']


def test_cli_markdown_mention(monkeypatch, fake_client):
    msg, row = _send(monkeypatch, fake_client, '- **@牟站通** 看下', ['7518957269125398529:牟站通', '200:张三'], True)
    assert msg['kind'] == 'markdown' and len(msg['mentions']) == 2
    assert row.content.startswith('@张三 - @牟站通 看下') and row.msg_type == 2
    assert json.loads(row.content_data)['_larkx_mention_ids'] == ['7518957269125398529', '200']
    action = Storage().list_actions()[-1]
    assert action.content == '@张三 - **@牟站通** 看下'


def test_agent_prefix_check_still_applies_with_mentions(monkeypatch, fake_client):
    from larkx import cli, state
    monkeypatch.setenv('LARKX_TRIGGER_PREFIX', '/ai')
    monkeypatch.setattr(cli, 'is_agent_actor', lambda: True)
    monkeypatch.setattr(state, 'read_turn', lambda: {'kind': 'message', 'is_owner': True, 'chat_id': 'target'})
    monkeypatch.setattr(cli, 'LarkAuth', lambda: object())
    monkeypatch.setattr(cli, 'LarkClient', lambda _: fake_client)
    for text, markdown in (('@牟站通 /ai 干活', False), ('@牟站通 **/ai** 干活', True), ('/ai 干活', False)):
        args = argparse.Namespace(chat_id='target', text=[text], root=None, json=True,
                                  mention=['7518957269125398529:牟站通'], markdown=markdown)
        assert cli.cmd_send(args) == 1
    assert not fake_client.replies or all(r['chat_id'] != 'target' for r in fake_client.replies)


def test_agent_signature_with_mentions(monkeypatch, fake_client):
    from larkx import cli, state
    monkeypatch.setenv('LARKX_REPLY_SIGNATURE', '数字分身')
    monkeypatch.setenv('LARKX_MIRROR', 'false')
    monkeypatch.setattr(cli, 'is_agent_actor', lambda: True)
    monkeypatch.setattr(state, 'read_turn', lambda: {'kind': 'message', 'is_owner': True, 'chat_id': 'target', 'ref': 'm'})
    msg, row = _send(monkeypatch, fake_client, '收到 @牟站通', ['7518957269125398529:牟站通'], True)
    assert msg['text'] == '收到 @牟站通\n\n—— 数字分身'
    r = _req(msg)
    assert [x[0] for x in _flat(r)][:2] == [TEXT, AT] and row.content == '收到 @牟站通\n\n—— 数字分身'
