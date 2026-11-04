---
name: larkx
description: 以本人的飞书账号（网页登录态）操作飞书 IM：给人或群发消息、转发消息、查聊天记录和历史、搜人和群、下载附件，以及指挥本人的飞书数字人（lark-twin）去做事、安排定时提醒、查看它发过什么。用户要求"在飞书上给某人发消息""看看某个群最近在聊什么""让我的数字人去……""提醒我……"时使用。飞书文档、表格、日历请用官方 lark-cli，不用本 skill。
---

# larkx：以本人身份操作飞书

命令都以本人的飞书账号执行。别人看到的发送者就是本人，所以发出去之前要和本人确认内容和对象。命令路径是 `{{LARKX}}`，下文简写为 `larkx`。加上 `--json` 后输出 JSON，失败时退出码非零。

## 找到会话

```bash
larkx chats --json                 # 最近活跃的会话：chat_id、name、chat_type（1 私聊，2 群，3 话题群）
larkx search <名字或群名>          # 搜人和群，输出 JSON
```

给某人发私聊时，先用 `search` 找到人。如果 `chats` 里已经有和这个人的私聊，就直接用它的 chat_id。

## 发消息

内容从 stdin 传入。结束标记用带引号的 `'EOF'`，这样内容里的特殊字符不会被 shell 解析：

```bash
larkx send <chat_id> --json - <<'EOF'
消息内容
EOF
```

需要富文本（代码块、列表、加粗/斜体、标题、引用、链接）时加 `-m`（`--markdown`），内容按 markdown 解析后以飞书富文本发出；不带 `-m` 时一律按纯文本发送，星号、反引号原样显示：

~~~bash
larkx send <chat_id> -m --json - <<'EOF'
# 标题（IM 里显示成加粗段落）

排查步骤：

```bash
larkx doctor
larkx service logs -n 50
```

1. 先看登录态
2. 再看日志

- **重点**：`/ai /takeover` 这类以 / 开头的内容会原样发出
- 文档：[飞书](https://www.feishu.cn) 或裸链接 https://www.feishu.cn

> 引用一段
EOF
~~~

支持的语法：围栏代码块（```` ```lang ````，带语法高亮，语言支持 bash/sh/python/go/sql/json/yaml 等，不认识的语言按 shell 高亮）、`#` 标题、`>` 引用、有序/无序列表、`**加粗**`、`*斜体*`、`[文字](http(s)链接)` 和裸链接。两处限制：行内 `` `代码` `` 飞书 IM 不支持，会保留文字但没有代码样式；markdown 链接的文字和链接分开展示（IM 没有自定义文字的超链接）。

回复某条消息或话题时加上 `--root <消息 id 或话题 id>`：普通群和私聊里是主会话里的引用回复（那条已经在话题里时回进话题），话题群里回进话题。发错了可以用 `larkx recall <msg_id>` 撤回，必须在飞书允许的撤回时限内。**发送前，要先和本人确认发给谁、发什么**，除非本人已经明确给出了对象和原文。

默认是普通发送：消息直接发到目标会话，对方看不到已读，但本人在那个会话的未读会清掉。本人想连未读也保住，可以在 lark-twin 的 `.env` 里打开 `LARKX_KEEP_UNREAD`（匿名发送：有未读时经本人私聊转发过去）。

## 转发消息

```bash
larkx forward <msg_id> --to <chat_id> --json                                  # 转发一条
larkx forward <msg_id> <msg_id> ... --to <chat_id> [--to <chat_id>] --json    # 逐条转发几条，按给的顺序；--to 可以写几个
larkx forward <msg_id> <msg_id> ... --merge --to <chat_id> --json             # 合并转发：几条合成一条"聊天记录"发出
```

**转发前，要先和本人确认转哪几条、转给谁**，除非本人已经明确给出。转发和发消息一样，别人看到的发送者是本人。

- `msg_id` 用 `larkx messages` / `larkx history` 里的数字 id，`chat_id` 用 `larkx chats` / `larkx search` 里的数字 id；`om_`、`oc_` 开头的开放平台 id 不能用。一次最多 50 条。
- 成功时输出 `{"ok": true, "merge": false, "messages": [{"chat_id": ..., "msg_id": ..., "source_ids": [...]}], "failed": []}`。`msg_id` 是目标会话里新消息的 id，转错了可以用 `larkx recall <msg_id>` 撤回。有没转出去的时候退出码非零，原因写在 `failed` 里，补转时只转这几条。
- 和发消息不同，转发不清掉本人的未读：目标会话里原有的未读还是未读，源消息的已读状态也不变，也不会让对方看到已读。
- 消息原样照搬，图片、文件也能转；`@` 会变成普通文字，对方收不到提醒。
- 合并转发只能合并同一个会话里的消息。卡片标题由飞书按源会话生成（群名，私聊是"A与B的会话"），不能自定义。
- 转发记录在 `larkx audit` 里。

## 看聊天记录

```bash
larkx messages <chat_id> --limit 50 --json    # 本地库，按时间从早到晚排列
larkx history <chat_id> --count 30 --json     # 从服务器拉最近的历史（本地库缺消息时用，加 --save 会存进本地库）
larkx download <msg_id> -o <保存路径>          # 下载图片或文件
```

## 指挥数字人

数字人是一个常驻的 agent 会话。它有长期记忆，以本人身份在飞书里回复带 `/ai` 前缀的消息。

```bash
larkx say --wait - <<'EOF'        # 以本人身份直接给数字人下指令，等它做完并打印回答
帮我看一下今天各个群里有没有人找我，整理成要点
EOF
larkx task add --in 30m - <<'EOF'  # 安排定时任务，到点后由数字人处理
提醒我给张三回电话（在我和自己的私聊里提醒）
EOF
larkx task add --at "10-22 09:00" --every 1d -   # 周期任务，内容同样从 stdin 读
larkx task list [--all] [--json]                  # 查看任务，--all 会包括已完成的
larkx task cancel <id>
larkx audit --limit 30                            # 通过 larkx 发出和转发的所有消息，标明是本人还是数字人发的，以及是哪一轮
larkx recall <msg_id>                             # 撤回一条消息（比如数字人误发、误转的那条，msg_id 在 audit 里）
```

### 让数字人替你聊一段（接管会话）

```bash
larkx takeover start <chat_id> --for 2h --goal "帮我和他约下周二下午开会"
larkx takeover list
larkx takeover stop <chat_id>     # 提前结束；结束后数字人会在你的私聊里发一份总结
```

接管期间，对方的每条消息都由数字人回复，它发出的每条也都会抄送到你的私聊。在飞书里也可以操作：在那个会话里发 `/ai /takeover 2h 目标` 开始，发 `/ai /release` 结束。

### 谁能唤起数字人（唤起白名单）

```bash
larkx whitelist list [--json]          # 当前规则和名单
larkx whitelist add <名字或 user_id>    # 搜人添加；同名时在终端里让本人选，不是终端时列出候选（部门、user_id），用 user_id 重跑
larkx whitelist remove <名字或 user_id>
```

规则：本人一直能唤起。白名单非空时，只有本人和白名单里的人能唤起，别人的 `/ai` 一律不理；白名单为空时，看 `LARKX_TRIGGER_OWNER_ONLY`（打开时只有本人）。群里照旧要 @ 本人。白名单里的人和其他人一样按访客对待，不会拿到本人的权限。改动立即生效，不用重启；网页控制台的"白名单"页和这里是同一份名单。白名单是权限设置，**加人、删人前都要和本人确认是哪一个人**（同名时报出部门和 user_id 让本人选）。数字人自己不能改白名单。没权限的人私聊发 `/ai`、或在群里 @ 本人发 `/ai` 时，daemon 会自动引用回复一句 `LARKX_DENY_REPLY`（同一会话同一人 10 分钟内只回一次），不经过数字人。

网页控制台的"设置"页可以打开"触发时先回一个表情"（默认关）：数字人被触发时，daemon 先以本人身份在那条消息上加一个 👌（OK），范围可选只对本人自己发的 `/ai`（默认）或所有触发数字人的消息。加表情会把那条消息标成已读，发消息的人能看到，删掉表情也撤不回；接管中的会话、纯控制命令、定时任务不加。只有本人能在网页上改，改了立即生效。

### 谁找过你

```bash
larkx mentions --since 24h        # 别人的私聊和 @你 的群消息；● 表示你之后还没在那个会话说过话
```

数字人的人设和行为准则在 `~/.larkx/agent/CLAUDE.md`，长期记忆在 `~/.larkx/agent/memory/`，这两处都可以直接查看和修改。

## 会话分类与清理

会话按分类管理，关注度三档：重点关注（全读、进记忆）、简单关注（只挑和本人有关的）、只存不看。要在 `.env` 里打开 `LARKX_CHAT_GROUPS`。

```bash
larkx groups                                         # 分类和会话数
larkx groups add 项目A --attention 重点关注 --keywords 发布
larkx groups assign <会话名或chat_id>... --to 项目A   # 归类; --attention 只存不看 单独设; --attention 跟分类 恢复
larkx groups profile                                 # 马上刷新会话画像
larkx chats --attention 重点关注 --active 7d          # 按关注度、活跃时间、--group 分类、--feishu-muted 免打扰筛
larkx prune --dry-run                                # 按保留天数清理旧消息, 先看会删多少; --days N --chat 会话 单独清
```

## 例行任务

```bash
larkx jobs                       # 每日回顾、每周整理记忆的上次结果和下次时间
larkx jobs log --job review      # 运行记录(失败原因在这里)
larkx jobs run review            # 让 daemon 马上跑一次; --preview 只看提示词
```

## 服务运维

```bash
larkx service status | logs -n 100 | restart | stop | start
larkx doctor          # 检查登录、配置、模型调用和服务状态
```

更完整的部署和排错流程，见仓库里的 `lark-twin` 运维 skill。

## 注意

- `larkx api <请求类型> <JSON>` 能调用任意网关接口，威力很大。只在其他命令做不到、并且本人同意时才用。
- 不要读取或输出 `~/.larkx/credentials.json`，那是本人飞书账号的完整登录态。
