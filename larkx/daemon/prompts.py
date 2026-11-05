"""daemon 交给数字人的维护任务和汇报的提示词。例行任务的提示词由 BUILDERS 按任务名生成, larkx jobs run --preview 也用它。"""

TAKEOVER_SUMMARY = ('接管「{name}」(chat_id={chat}) 已结束({why})。用 larkx messages {chat} --limit 60 回顾接管期间的对话, '
                    '给本人发一份简短总结: 聊了什么、谈成了什么、还有什么等本人决定或跟进。目标是: {goal}')
FLUSH_PROMPT = ('上下文快满了, 马上会被压缩, 压缩后早期的细节会丢失。现在把这段时间里值得长期记住、但还没写进 memory/ 的内容整理进去'
                '(人、偏好、约定、待跟进的事、群的背景), 更新 memory/MEMORY.md 索引。不要发飞书消息, 整理完直接结束。')
REVIEW_PROMPT = ('每日回顾(最近 24 小时)。下面是有动静的会话, 按关注度处理, 用 larkx messages <chat_id> --since 24h 看:\n'
                 '- 重点关注: 逐个看, 值得长期记住的整理进 memory/(人、群、事、约定、待跟进);\n'
                 '- 简单关注: 也看, 但只记和本人有关的: @本人、提到本人负责的事或分类关键词、需要本人处理的;\n'
                 '- 标了「私人」的: 只看有没有需要本人处理的事, 内容不写进记忆;\n'
                 '- 「免打扰」只是本人在飞书里的提醒设置, 照样按关注度看, 和本人有关的尤其不能漏;\n'
                 '- 只存不看的会话已经跳过, 不要去看。\n'
                 '{chats}\n'
                 '然后: 记忆里过时或错误的改掉, 已经办完的跟进删掉, 更新 memory/MEMORY.md 索引; '
                 '本人答应过别人、还没安排的跟进, 用 larkx task add --at <时间> 安排提醒(会在本人私聊提醒)。')
CONSOLIDATE_PROMPT = ('每周整理记忆: 通读 memory/ 下的全部文件。合并主题重复的文件; 删掉已经办完的跟进、过期的安排和不再成立的内容; '
                      '每条事实注明最后确认的日期; 超过 60 天没再出现、也不再相关的内容移到 memory/archive/ 并从索引里去掉; '
                      'MEMORY.md 一个主题一行, 保持在 60 行以内。')
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


def review(cfg: dict, storage, now, owner_id: str = '') -> str:
    if not cfg.get('chat_groups'):
        return BASIC_REVIEW_PROMPT
    from .. import groups
    rows = groups.activity(storage, owner_id, int(now.timestamp() - 86400))
    shown = [r for r in rows if r['attention'] != 'archive']
    skipped = len(rows) - len(shown)
    chats = '\n'.join(chat_line(r) for r in shown) if shown else '(最近 24 小时没有需要回顾的会话)'
    if skipped:
        chats += f'\n(另有 {skipped} 个只存不看的会话有消息, 已跳过)'
    return REVIEW_PROMPT.format(chats=chats) + MAINTENANCE_TAIL


def consolidate(cfg: dict, storage, now, owner_id: str = '') -> str:
    return CONSOLIDATE_PROMPT + MAINTENANCE_TAIL


BUILDERS = {'review': review, 'consolidate': consolidate}
