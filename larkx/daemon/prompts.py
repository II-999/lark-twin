"""daemon 交给数字人的维护任务和汇报的提示词。例行任务的提示词由 BUILDERS 按任务名生成, larkx jobs run --preview 也用它。"""

TAKEOVER_SUMMARY = ('接管「{name}」(chat_id={chat}) 已结束({why})。用 larkx messages {chat} --limit 60 回顾接管期间的对话, '
                    '给本人发一份简短总结: 聊了什么、谈成了什么、还有什么等本人决定或跟进。目标是: {goal}')
FLUSH_PROMPT = ('上下文快满了, 马上会被压缩, 压缩后早期的细节会丢失。现在把这段时间里值得长期记住、但还没写进 memory/ 的内容整理进去'
                '(人、偏好、约定、待跟进的事、群的背景), 更新 memory/MEMORY.md 索引。不要发飞书消息, 整理完直接结束。')
REVIEW_PROMPT = ('每日回顾(最近 24 小时)。下面是有动静的会话, 按关注度处理, 用 larkx messages <chat_id> --since 24h 看:\n'
                 '- 重点关注: 逐个看, 值得长期记住的整理进 memory/(人、群、事、约定、待跟进);\n'
                 '- 简单关注: 也看, 但只记和本人有关的: @本人、提到本人负责的事或组关键词、需要本人处理的;\n'
                 '- 标了「私人」的: 只看有没有需要本人处理的事, 内容不写进记忆;\n'
                 '- 「免打扰」只是本人在飞书里的提醒设置, 照样按关注度看, 和本人有关的尤其不能漏;\n'
                 '- 只存不看的会话已经跳过, 不要去看。\n'
                 '{chats}\n'
                 '然后: 记忆里过时或错误的改掉, 已经办完的跟进删掉, 更新 memory/MEMORY.md 索引; '
                 '本人答应过别人、还没安排的跟进, 用 larkx task add --at <时间> 安排提醒(会在本人私聊提醒)。')
CONSOLIDATE_PROMPT = ('每周整理记忆: 通读 memory/ 下的全部文件。合并主题重复的文件; 删掉已经办完的跟进、过期的安排和不再成立的内容; '
                      '每条事实注明最后确认的日期; 超过 60 天没再出现、也不再相关的内容移到 memory/archive/ 并从索引里去掉; '
                      'MEMORY.md 一个主题一行, 保持在 60 行以内。')
BOOTSTRAP_PROMPT = ('初始化记忆(第 {i}/{n} 批)。刚从飞书回填了本人最近 {days} 天的聊天, 先了解本人的工作。'
                    '下面是本人重点关注的会话, 逐个用 larkx messages <chat_id> --since {days}d --limit 200 看:\n{chats}\n'
                    '整理进 memory/: 我.md(本人的角色、负责的事、常合作的人和协作方式), 人-<名字>.md(和本人的关系、在做什么、'
                    '习惯和偏好), 群-<群名>.md(这个群做什么、有哪些人、本人在里面的角色), 事-<主题>.md(进行中的工作和待跟进, '
                    '写清楚谁、做什么、什么时候)。只记稳定有用的事实, 每条写上日期; 已有同主题的文件就合并更新, 不要重复新建; '
                    '更新 memory/MEMORY.md 索引。{last}')
BOOTSTRAP_LAST = '这是最后一批: 最后通读一遍 memory/, 把重复的合并, 索引整理好。'
BRIEFING_PROMPT = ('写今天的工作简报, 发到本人私聊。下面是代码算好的数据(都是事实, 别编造):\n\n{data}\n\n'
                   '要求: markdown, 一条消息, 25 行以内, 先写结论。顺序: ① 今天要你处理的(找你还没回的、你标记的、'
                   '到期的提醒, 再结合 memory/ 里 事-*.md 的待跟进); ② 重点关注的会话进展, 每个一两句, 需要时用 '
                   'larkx messages <chat_id> --since {since} 看; ③ 简单关注和免打扰的群里和本人有关的; ④ 提醒。{weekly}'
                   '没什么可报的也发一句"今天没有需要你处理的事"。发现某个会话分组明显不合适, 可以在末尾建议'
                   '(如: 「xx群」消息多但和你无关, 建议设为只存不看)。')
BRIEFING_WEEKLY = '今天是周一, 末尾加「上周回顾 / 本周关注」两三条。'
# 维护任务的共同结尾: 不对外发消息, 最后一句话总结存进运行记录
MAINTENANCE_TAIL = ' 不要发飞书消息。最后用一两句话说明这次记了或改了什么(会存进运行记录)。'


def chat_line(r: dict) -> str:
    """会话一行: [关注度] 名字 (chat_id) 组 · 24h 条数 · 本人发言 · @本人 · 标记。"""
    tags = [r['attention_name']] + (['私人'] if r.get('private') else []) + (['免打扰'] if r.get('feishu_muted') else [])
    parts = [f"24h {r['recent']} 条"]
    if r.get('recent_owner'):
        parts.append(f"本人 {r['recent_owner']} 条")
    if r.get('recent_at_me'):
        parts.append(f"@本人 {r['recent_at_me']} 次")
    group = f" 组「{r['group'] or r['suggested_group']}」" if (r['group'] or r['suggested_group']) else ''
    return f"- [{'/'.join(tags)}] {r['name'] or '(未命名)'} ({r['chat_id']}){group}: {', '.join(parts)}"


def review(cfg: dict, storage, now, owner_id: str = '', **kw) -> str:
    from .. import groups
    rows = groups.activity(storage, owner_id, int(now.timestamp() - 86400))
    shown = [r for r in rows if r['attention'] != 'archive']
    skipped = len(rows) - len(shown)
    chats = '\n'.join(chat_line(r) for r in shown) if shown else '(最近 24 小时没有需要回顾的会话)'
    if skipped:
        chats += f'\n(另有 {skipped} 个只存不看的会话有消息, 已跳过)'
    return REVIEW_PROMPT.format(chats=chats) + MAINTENANCE_TAIL


def consolidate(cfg: dict, storage, now, owner_id: str = '', **kw) -> str:
    return CONSOLIDATE_PROMPT + MAINTENANCE_TAIL


def bootstrap(cfg: dict, storage, now, owner_id: str = '', **kw) -> str:
    from .. import groups, jobs
    from .maintenance import BOOTSTRAP_BATCH, bootstrap_chats
    st = jobs.JobStore(storage).get_state('bootstrap')
    ids = st.get('chats') or bootstrap_chats(storage)
    n = max(1, -(-len(ids) // BOOTSTRAP_BATCH))
    i = min(st.get('next', 0), n - 1)
    rows = {r['chat_id']: r for r in groups.ChatStore(storage).view(ids[i * BOOTSTRAP_BATCH:(i + 1) * BOOTSTRAP_BATCH])}
    lines = []
    for cid in ids[i * BOOTSTRAP_BATCH:(i + 1) * BOOTSTRAP_BATCH]:
        r = rows[cid]
        lines.append(f"- {r['name'] or '(未命名)'} ({cid}) 组「{r['group'] or r['suggested_group']}」: "
                     f"{r['msgs']} 条, 本人 {r['owner_msgs']} 条")
    return BOOTSTRAP_PROMPT.format(i=i + 1, n=n, days=int(cfg.get('backfill_days') or 30), chats='\n'.join(lines),
                                   last=BOOTSTRAP_LAST if i == n - 1 else '') + MAINTENANCE_TAIL


def briefing(cfg: dict, storage, now, owner_id: str = '', client=None, **kw) -> str:
    from .. import digest
    data = digest.briefing_data(storage, owner_id, now, client=client)
    return BRIEFING_PROMPT.format(data=digest.render(data), since='7d' if data['weekly'] else '24h',
                                  weekly=BRIEFING_WEEKLY if data['weekly'] else '')


BUILDERS = {'review': review, 'consolidate': consolidate, 'bootstrap': bootstrap, 'briefing': briefing}
