"""daemon 交给数字人的维护任务和汇报的提示词。例行任务的提示词由 BUILDERS 按任务名生成, larkx jobs run --preview 也用它。"""

TAKEOVER_SUMMARY = ('接管「{name}」(chat_id={chat}) 已结束({why})。用 larkx messages {chat} --limit 60 回顾接管期间的对话, '
                    '给本人发一份简短总结: 聊了什么、谈成了什么、还有什么等本人决定或跟进。目标是: {goal}')
FLUSH_PROMPT = ('上下文快满了, 马上会被压缩, 压缩后早期的细节会丢失。现在把这段时间里值得长期记住、但还没写进 memory/ 的内容整理进去'
                '(人、偏好、约定、待跟进的事、群的背景), 更新 memory/MEMORY.md 索引。不要发飞书消息, 整理完直接结束。')
REVIEW_PROMPT = ('每日回顾: 用 larkx chats 和 larkx messages 看一下最近 24 小时本人参与的会话(重点是本人发的消息和找本人的消息), '
                 '把值得长期记住的新信息整理进 memory/ 并更新索引; 发现记忆里过时或错误的内容就改掉, 已经办完的跟进删掉。'
                 '本人答应过别人、还没安排的跟进, 用 larkx task add --at <时间> 安排提醒(会在本人私聊提醒)。')
CONSOLIDATE_PROMPT = ('每周整理记忆: 通读 memory/ 下的全部文件。合并主题重复的文件; 删掉已经办完的跟进、过期的安排和不再成立的内容; '
                      '每条事实注明最后确认的日期; 超过 60 天没再出现、也不再相关的内容移到 memory/archive/ 并从索引里去掉; '
                      'MEMORY.md 一个主题一行, 保持在 60 行以内。')
# 维护任务的共同结尾: 不对外发消息, 最后一句话总结存进运行记录
MAINTENANCE_TAIL = ' 不要发飞书消息。最后用一两句话说明这次记了或改了什么(会存进运行记录)。'


def review(cfg: dict, storage, now) -> str:
    return REVIEW_PROMPT + MAINTENANCE_TAIL


def consolidate(cfg: dict, storage, now) -> str:
    return CONSOLIDATE_PROMPT + MAINTENANCE_TAIL


BUILDERS = {'review': review, 'consolidate': consolidate}
