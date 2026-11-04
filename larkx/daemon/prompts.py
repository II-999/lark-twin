"""daemon 交给数字人的维护任务和汇报的提示词。例行任务的提示词由 BUILDERS 按任务名生成, larkx jobs run --preview 也用它。"""

TAKEOVER_SUMMARY = ('接管「{name}」(chat_id={chat}) 已结束({why})。用 larkx messages {chat} --limit 60 回顾接管期间的对话, '
                    '给本人发一份简短总结: 聊了什么、谈成了什么、还有什么等本人决定或跟进。目标是: {goal}')
FLUSH_PROMPT = ('上下文快满了, 马上会被压缩, 压缩后早期的细节会丢失。现在把这段时间里值得长期记住、但还没写进 memory/ 的内容整理进去'
                '(人、偏好、约定、待跟进的事、群的背景), {index}。不要发飞书消息, 整理完直接结束。')
# 结构化记忆(LARKX_MEMORY_STRUCTURED)打开时: 文件开头写字段, MEMORY.md 由代码生成
MEMORY_FORMAT = ('记忆文件的格式: 一个主题一个文件, 命名 我.md / 人-<名字>.md / 群-<群名>.md / 事-<主题>.md; 开头写字段 '
                 '---\ntype: 我|人|群|事\nname: 名字\ndesc: 一句话说明\npeople: [相关的人]\nchats: [相关的群]\n'
                 'status: 进行中|待跟进|已完成(只用于事)\ndue: YYYY-MM-DD(只用于事, 可空)\nupdated: YYYY-MM-DD\n---, '
                 '正文自由写。memory/MEMORY.md 由代码按字段生成, 不用改。写完运行 larkx memory lint, 有错误就改掉。')
REVIEW_PROMPT = ('每日回顾(最近 24 小时)。下面是有动静的会话, 按关注度处理, 用 larkx messages <chat_id> --since 24h 看:\n'
                 '- 重点关注: 逐个看, 值得长期记住的整理进 memory/(人、群、事、约定、待跟进);\n'
                 '- 简单关注: 也看, 但只记和本人有关的: @本人、提到本人负责的事或分类关键词、需要本人处理的;\n'
                 '- 标了「私人」的: 只看有没有需要本人处理的事, 内容不写进记忆;\n'
                 '- 「免打扰」只是本人在飞书里的提醒设置, 照样按关注度看, 和本人有关的尤其不能漏;\n'
                 '- 只存不看的会话已经跳过, 不要去看。\n'
                 '{chats}\n'
                 '然后: 记忆里过时或错误的改掉, {tidy}; '
                 '本人答应过别人、还没安排的跟进, 用 larkx task add --at <时间> 安排提醒(会在本人私聊提醒)。')
CONSOLIDATE_PROMPT = ('每周整理记忆: 通读 memory/ 下的全部文件。合并主题重复的文件; 删掉已经办完的跟进、过期的安排和不再成立的内容; '
                      '每条事实注明最后确认的日期; 超过 60 天没再出现、也不再相关的内容移到 memory/archive/ 并从索引里去掉; '
                      'MEMORY.md 一个主题一行, 保持在 60 行以内。')
CONSOLIDATE_STRUCTURED = ('每周整理记忆: 先运行 larkx memory stats 和 larkx memory lint, 再通读 memory/。合并主题重复的文件; '
                          '删掉过期的安排和不再成立的内容; 已完成超过 7 天的事、超过 60 天没再确认也不再相关的内容移到 '
                          'memory/archive/; 还没有开头字段的旧文件补上字段; 记忆文件保持在 60 个以内(我.md 不归档)。')
BOOTSTRAP_PROMPT = ('初始化记忆(第 {i}/{n} 批)。刚从飞书回填了本人最近 {days} 天的聊天, 先了解本人的工作。'
                    '下面是{which}, 逐个用 larkx messages <chat_id> --since {days}d --limit 200 看:\n{chats}\n'
                    '整理进 memory/: 本人的角色、负责的事、常合作的人和协作方式; 常打交道的人(和本人的关系、在做什么、习惯和偏好); '
                    '群(做什么、有哪些人、本人在里面的角色); 进行中的工作和待跟进(写清楚谁、做什么、什么时候)。'
                    '只记和工作有关、稳定有用的事实: 不记别人的私事, 不抄聊天原文, 用自己的话概括; '
                    '记忆会存进数字人目录、别人唤起数字人时也可能读到。已有同主题的文件就合并更新, 不要重复新建。{last}')
BOOTSTRAP_LAST = '这是最后一批: 最后通读一遍 memory/, 把重复的合并。'
BOOTSTRAP_FINAL = '初始化记忆收尾: 各批会话已经整理过了(有的批次没成功, 跳过了)。通读一遍 memory/, 把主题重复的文件合并。'
BRIEFING_PROMPT = ('写今天的工作简报, 发到本人私聊。下面是代码算好的数据(都是事实, 别编造):\n\n{data}\n\n'
                   '要求: markdown, 一条消息, 25 行以内, 先写结论。顺序: ① 今天要你处理的(找你还没回的、你标记的、'
                   '到期的提醒和待跟进的事); ② {chats}进展, 每个一两句, 需要时用 larkx messages <chat_id> --since {since} 看; '
                   '{related}④ 提醒。{weekly}没什么可报的也发一句"今天没有需要你处理的事"。{suggest}')
BRIEFING_GROUPED = {'chats': '重点关注的会话', 'related': '③ 简单关注和免打扰的群里和本人有关的; ',
                    'suggest': '发现某个会话分类明显不合适, 可以在末尾建议(如: 「xx群」消息多但和你无关, 建议设为只存不看)。'}
BRIEFING_PLAIN = {'chats': '本人参与的活跃会话', 'related': '③ 免打扰的群里和本人有关的; ', 'suggest': ''}
BRIEFING_WEEKLY = '今天是周一, 末尾加「上周回顾 / 本周关注」两三条。'
ORGANIZE_SCHEMA = '''{
  "summary": "一句话说明这次整理了什么",
  "groups": [{"name": "项目A", "attention": "重点关注", "private": false, "keywords": ["发布"],
              "chats": ["会话名或 chat_id", ...], "reason": "为什么"}],
  "labels": true,
  "pin": [{"chat": "会话名或 chat_id", "reason": "..."}],
  "unpin": [{"chat": "...", "reason": "..."}],
  "pin_order": ["置顶从上到下的会话名或 chat_id", ...],
  "flag": [{"chat": "...", "msg_id": "消息 id", "text": "消息摘要", "reason": "..."}],
  "mute": ["会话名或 chat_id", ...], "mute_reason": "..."
}'''
ORGANIZE_PROMPT = ('{head}先读 memory/(尤其 我.md、事-*.md)了解本人的工作, 再按下面的现状写一份整理方案, 交给本人确认:\n'
                   '1. 分类: 按本人的业务线、团队、项目划分(例如「团队核心」「某业务」「会议临时」「部门周知」「技术圈」'
                   '「需要你处理的通知」「私人」), 每个分类定关注度: 重点关注(全读、进记忆) / 简单关注(只挑和本人有关的) / '
                   '只存不看。私人分类写 private; 可以给分类加关键词(简单关注时提到这些词也算和本人有关)。已有的分类沿用名字, '
                   '只写需要变的。{scope}\n'
                   '2. 置顶: 重点关注里常用却没置顶的建议置顶; 置顶了但 30 天没动静或明显无关的建议取消; 给出按分类排的置顶顺序 '
                   'pin_order。本人私人的置顶不要动。\n'
                   '3. 标记: 找本人还没回、需要本人处理的消息, 建议标记(要写 msg_id, 用 larkx messages <chat_id> 查)。\n'
                   '4. 免打扰: 只存不看、现在还会提醒本人的大群, 建议免打扰; 重点关注的不要免打扰。\n'
                   '每条都要有理由, 没必要的改动不要提。写成 JSON 存到 downloads/organize.json, 运行 '
                   'larkx organize submit downloads/organize.json(报错就按提示改了再提交)。方案由系统私信给本人, '
                   '你不用发。{none}JSON 格式:\n{schema}\n\n现状:\n{context}')
ORGANIZE_INIT_HEAD = '初始整理会话: 刚回填完最近的聊天并初始化了记忆, 现在给本人的全部会话做一次分类。'
ORGANIZE_WEEKLY_HEAD = '每周整理会话: 看看分类、置顶、标记要不要调整。'
# 维护任务的共同结尾: 不对外发消息, 最后一句话总结存进运行记录
MAINTENANCE_TAIL = ' 不要发飞书消息。最后用一两句话说明这次记了或改了什么(会存进运行记录)。'


def chat_line(r: dict) -> str:
    """会话一行: [关注度] 名字 (chat_id) 分类 · 24h 条数 · 本人发言 · @本人 · 标记。"""
    tags = [r['attention_name']] + (['私人'] if r.get('private') else []) + (['免打扰'] if r.get('feishu_muted') else [])
    parts = [f"24h {r['recent']} 条"]
    if r.get('recent_owner'):
        parts.append(f"本人 {r['recent_owner']} 条")
    if r.get('recent_at_me'):
        parts.append(f"@本人 {r['recent_at_me']} 次")
    group = f" 分类「{r['group'] or r['suggested_group']}」" if (r['group'] or r['suggested_group']) else ''
    return f"- [{'/'.join(tags)}] {r['name'] or '(未命名)'} ({r['chat_id']}){group}: {', '.join(parts)}"


# 没开会话分类时用的每日回顾: 和原来的一字不差
BASIC_REVIEW_PROMPT = ('每日回顾: 用 larkx chats 和 larkx messages 看一下最近 24 小时本人参与的会话(重点是本人发的消息和找本人的消息), '
                       '把值得长期记住的新信息整理进 memory/ 并更新索引; 发现记忆里过时或错误的内容就改掉。'
                       '答应过但还没安排的跟进, 用 larkx task add 补上。不要发飞书消息, 整理完直接结束。')

# 打开结构化记忆时的每日回顾(没开会话分类)
BASIC_REVIEW_STRUCTURED = ('每日回顾: 用 larkx chats 和 larkx messages 看一下最近 24 小时本人参与的会话(重点是本人发的消息和找本人的消息), '
                           '把值得长期记住的新信息整理进 memory/; 发现记忆里过时或错误的内容就改掉, 办完的事把 status 改成已完成。'
                           '本人答应过别人、还没安排的跟进, 用 larkx task add --at <时间> 安排提醒(会在本人私聊提醒)。')

def _structured(cfg: dict) -> bool:
    return bool(cfg.get('memory_structured'))


def _with_format(cfg: dict, text: str) -> str:
    return text + '\n' + MEMORY_FORMAT if _structured(cfg) else text


def flush_prompt(cfg: dict) -> str:
    """上下文快满时整理记忆的提示词。"""
    return _with_format(cfg, FLUSH_PROMPT.format(
        index='按记忆文件的格式写好开头的字段' if _structured(cfg) else '更新 memory/MEMORY.md 索引'))


def review(cfg: dict, storage, now, owner_id: str = '', **kw) -> str:
    st = _structured(cfg)
    if not cfg.get('chat_groups'):
        if not st:
            return BASIC_REVIEW_PROMPT
        return _with_format(cfg, BASIC_REVIEW_STRUCTURED) + MAINTENANCE_TAIL
    from .. import groups
    rows = groups.activity(storage, owner_id, int(now.timestamp() - 86400))
    shown = [r for r in rows if r['attention'] != 'archive']
    skipped = len(rows) - len(shown)
    chats = '\n'.join(chat_line(r) for r in shown) if shown else '(最近 24 小时没有需要回顾的会话)'
    if skipped:
        chats += f'\n(另有 {skipped} 个只存不看的会话有消息, 已跳过)'
    tidy = '办完的事把 status 改成已完成' if st else '已经办完的跟进删掉, 更新 memory/MEMORY.md 索引'
    return _with_format(cfg, REVIEW_PROMPT.format(chats=chats, tidy=tidy)) + MAINTENANCE_TAIL


def consolidate(cfg: dict, storage, now, owner_id: str = '', **kw) -> str:
    return _with_format(cfg, CONSOLIDATE_STRUCTURED if _structured(cfg) else CONSOLIDATE_PROMPT) + MAINTENANCE_TAIL


def bootstrap(cfg: dict, storage, now, owner_id: str = '', **kw) -> str:
    from .. import groups, jobs
    from .maintenance import BOOTSTRAP_BATCH, bootstrap_chats
    st = jobs.JobStore(storage).get_state('bootstrap')
    ids = st.get('chats') or bootstrap_chats(storage, cfg, owner_id)
    n = max(1, -(-len(ids) // BOOTSTRAP_BATCH))
    if st.get('next', 0) >= n:  # 收尾: 最后一批被跳过时补做"通读合并"
        return _with_format(cfg, BOOTSTRAP_FINAL) + MAINTENANCE_TAIL
    i = min(st.get('next', 0), n - 1)
    batch = ids[i * BOOTSTRAP_BATCH:(i + 1) * BOOTSTRAP_BATCH]
    days = int(cfg.get('backfill_days') or 30)
    cs = groups.ChatStore(storage)
    rows = {r['chat_id']: r for r in cs.view(batch)}
    stats = cs.stats(owner_id, storage.owner_name(owner_id), int(now.timestamp() - days * 86400))
    lines = []
    for cid in batch:
        r, s = rows[cid], stats.get(cid, {})
        group = (r['group'] or r['suggested_group']) if cfg.get('chat_groups') else ''
        lines.append(f"- {r['name'] or '(未命名)'} ({cid})" + (f" 分类「{group}」" if group else '')
                     + f": {s.get('msgs', 0)} 条, 本人 {s.get('owner_msgs', 0)} 条")
    which = '本人重点关注的会话' if cfg.get('chat_groups') else '本人发言最多的会话'
    return _with_format(cfg, BOOTSTRAP_PROMPT.format(i=i + 1, n=n, days=days, which=which, chats='\n'.join(lines),
                                                     last=BOOTSTRAP_LAST if i == n - 1 else '')) + MAINTENANCE_TAIL


def briefing(cfg: dict, storage, now, owner_id: str = '', client=None, **kw) -> str:
    from .. import digest
    data = digest.briefing_data(storage, owner_id, now, client=client, home=cfg.get('agent_home'), cfg=cfg)
    parts = BRIEFING_GROUPED if cfg.get('chat_groups') else BRIEFING_PLAIN
    return BRIEFING_PROMPT.format(data=digest.render(data), since='7d' if data['weekly'] else '24h',
                                  weekly=BRIEFING_WEEKLY if data['weekly'] else '', **parts)


def _organize(head, scope, none):
    def build(cfg: dict, storage, now, owner_id: str = '', client=None, **kw) -> str:
        from .. import organize
        return ORGANIZE_PROMPT.format(head=head, scope=scope, none=none, schema=ORGANIZE_SCHEMA,
                                      context=organize.context(storage, client, owner_id)) + MAINTENANCE_TAIL
    return build


BUILDERS = {'review': review, 'consolidate': consolidate, 'bootstrap': bootstrap, 'briefing': briefing,
            'organize_init': _organize(ORGANIZE_INIT_HEAD, '每个会话都要归到一个分类(机器人推送、大群可以放进只存不看的分类)。', ''),
            'organize': _organize(ORGANIZE_WEEKLY_HEAD, '重点看新会话(未分类的)和关注度明显不合适的。',
                                  '这周没有需要调整的就不提交, 直接说明即可。')}
