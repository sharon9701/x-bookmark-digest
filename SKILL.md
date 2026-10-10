---
name: x-bookmark-digest
description: >
  把你在 X 上点过喜欢、存进书签的内容，变成每天可消化的个人阅读收件箱：
  只抓 Bookmarks 和 Likes，按新增内容去重，保留原贴标题与可用配图，
  每次推出一张交互卡片，让你直接选择 read、keep、纳入 Obsidian 或调整分类。
  适用于本人或已授权的 X 账号；不读取时间线，不自动发布，也不默认移出 X 内容。
---

# X Bookmarks + Likes Digest

这是一个带本地状态、可恢复队列和聊天交互的 X 阅读队列 Skill。核心规则在本文件，细节按需读取 references；确定性的状态变更优先调用 `scripts/x_bookmark_digest.py`，不要让 Agent 手工改 SQLite 或 session JSON。

## 试跑反馈与修改协议

试跑发现某类结果有问题时，不得只围绕触发反馈的案例加特判。先抽象共同机制，再修改对应的规则层；随后分析所有受影响的输入、模式、边界、宿主和副作用，并用覆盖不同方向的 3–5 个场景回归。原案例通过只是必要条件。完整流程与记录模板见 [references/modification-protocol.md](references/modification-protocol.md)。

## 适用范围

- 只读取当前已验证账号的 `bookmarks` 和 `likes` 两个栏目；同一推文按 ID 合并，保留全部来源。
- 不读取时间线、搜索、通知、公开账号收藏；不自动点赞、书签、发布、回复或转发。
- 不输出“为什么值得读”。进入用户书签或喜欢栏目本身就是筛选信号。
- 每日 digest 默认只处理上一自然日首次同步发现的新增条目；不会把账号已有的全部收藏重新翻出。
- 当前 `opencli` 只返回收藏/喜欢列表，没有“何时加入书签/点喜欢”的可靠时间戳，因此 `first_seen_at` 是本地发现时间代理；要做到严格事件时间，适配器必须提供 `source_added_at`。
- `sync` 输出里的 `new` 只表示本地首次出现的推文 ID，不等于今天新点了多少喜欢；同时查看 `new_source_memberships` 和 `pending.diagnostics`。
- `1`、`2`、`3`、`4` 是唯一用户动作；默认一次只处理一条，不要求用户选择第几条。

## 推送时间门槛

每日自动化启动时，先读取 `defaults.schedule`：

- 已配置 `time` 时，按该时间和 `defaults.timezone` 执行，不重复询问；
- 未配置 `time` 时，必须先询问用户希望的推送时间、时区和频率，例如“每天 09:00（Asia/Shanghai）”；等待用户回答后，再保存到自动化配置并继续执行；
- 用户明确说“现在推送”或“试跑一次”时，视为手动运行，直接执行一次，不要求先设置长期时间；
- 用户要求修改时间时，更新自动化配置，下一次按新时间运行。

时间设置只决定何时运行，不改变内容窗口：每日运行仍只处理上一自然日新增的条目。

## 执行门槛

执行 `sync` 前必须完成：

1. `opencli` 在 `PATH` 中；
2. `opencli twitter --help` 同时包含 `bookmarks` 和 `likes`；
3. `opencli twitter whoami -f json` 返回已登录用户名；
4. 配置了 `expected_username` 时，实际账号必须匹配。

缺少 CLI、登录态、配置或账号不匹配时停止，不自动登录、不猜账号、不把失败当成“没有新增”。先运行：

```bash
python3 scripts/doctor.py
```

安装和跨 Agent 配置见 [references/portability.md](references/portability.md)。

## 安全流水线

按以下顺序执行：

```text
doctor → sync → pending --preview --window previous_day → AI 注释校验
→ review-start --claim → cards/card → review-apply 或 review-batch
```

- 首次同步会自动建立历史基线：已有收藏进入 `backlog`，只作为历史保留，不进入每日提醒；之后新发现的条目才进入 `unread`。
- 如果是旧版本已经存在本地条目的数据库，Skill 不会自动重置提醒时间；先完成一次明确的基线迁移，再开始每日自动化。
- `pending --preview --window previous_day` 只读取上一自然日窗口，不占用提醒窗口；注释和队列校验成功后才 `review-start --claim`。需要滚动 24 小时时才显式使用 `--window rolling`。
- 如果 diagnostics 显示今天有本地新观察但上一自然日窗口为空，只能说明当前接口无法确认 X 的真实操作日期，不得回复“用户昨天没有新增”。
- `review-start` 会冻结队列指纹；已有 session 默认拒绝覆盖，只有明确使用 `--replace` 才能重建。
- 用户决定必须通过 `review-apply` 或 `review-batch` 写入；不要先 `mark` 再手工 `review-record`，避免数据库和 receipt 分叉。
- 注释、队列、session 不匹配时停止；不要用 `--force` 掩盖状态冲突。
- claim、receipt、外部副作用和恢复规则见 [references/state-and-safety.md](references/state-and-safety.md)。

示例：

```bash
python3 scripts/x_bookmark_digest.py sync --limit 200
python3 scripts/x_bookmark_digest.py pending --preview --window previous_day \
  --output runs/pending.json
python3 scripts/x_bookmark_digest.py render \
  --annotations runs/annotations.json --output runs/digest.md
python3 scripts/x_bookmark_digest.py review-start \
  --annotations runs/annotations.json --session runs/review-session.json --claim
```

## 聊天交互

默认使用 `single` 模式：每次只发送队列中的下一条卡片，等待用户回复一个动作后才发送下一条。不要同时发送多条卡片，也不要询问用户“选择第几条”。只有用户明确要求批量时，才切换为 `batch`。

```bash
python3 scripts/x_bookmark_digest.py card \
  --annotations runs/annotations.json \
  --session runs/review-session.json
```

卡片必须保留：原贴标题、作者、分类、来源、原帖链接、媒体提示、摘要、要点和四个动作。默认不在聊天内嵌 X 远程图片：图片 CDN 或宿主远程媒体加载不稳定时，只显示配图数量提示并保留原帖/媒体链接，避免破图占位符。只有用户或宿主明确确认远程图片可稳定加载时，才在 `defaults.inline_media_previews` 设为 `true` 后尝试内嵌；不伪造本地图片。格式契约见 [references/interaction-contract.md](references/interaction-contract.md)。

默认每次只回复当前卡片的一个动作：

```text
2
```

用户回复 `1`、`2`、`3` 或 `4 分类名` 后，Agent 应将当前卡片的决定写入队列，再调用 `card --session` 推送下一条。不要要求用户在回复中标记卡片序号。

批量模式是显式的高级选项。只有用户主动要求批量时，Agent 才使用 `cards`、多行决定和 `review-batch`：

Agent 应将回复解析为有序 decisions JSON，再调用：

```bash
python3 scripts/x_bookmark_digest.py parse-reply \
  --session runs/review-session.json \
  --reply runs/user-reply.txt \
  --count 3 \
  --output runs/decisions.json
```

然后调用：

```bash
python3 scripts/x_bookmark_digest.py review-batch \
  --annotations runs/annotations.json \
  --session runs/review-session.json \
  --decisions runs/decisions.json
```

`review-batch` 按顺序执行；任一条失败就停止，后续决定不执行，并保留当前队列位置。默认单卡片则使用 `review-apply`。动作语义固定为：

1. `read`：移出当前存在的书签/喜欢来源；需要带 `--confirm READ:<ID>`；成功后不再提醒。
2. `keep`：状态设为 `keep`，更新提醒时间，保留到下一个 digest 窗口。
3. `obsidian`：已配置 Vault 时写入知识库；未配置时生成 `obsidian-pending/` 待导入 Markdown；成功处理后不再提醒。
4. `category`：必须带合法分类，更新分类并保持当前阅读状态。

分类名称从当前 `config.json` 的 `categories` 读取；分类、摘要、要点和建议动作按 [references/annotation-contract.md](references/annotation-contract.md) 校验。内部长度限制只用于校验，不写进正式推送文案。

## 远程修改规则

- `read` 是唯一会触发 `unbookmark`/`unlike` 的聊天动作；用户明确回复 `1` 后，Agent 才能生成对应 `READ:<ID>` 确认。
- 自动化任务不执行 `read`、远程移除或 Obsidian 写入。
- 移除按来源逐个记录；部分成功时保留失败来源并停止，不把部分成功报告为全部完成。
- 不向日志、Markdown 或回复中输出 Cookie、Token 或完整认证信息。

## 今日自动化

每日自动化在完成推送时间门槛后，使用配置时区执行 `doctor → sync → pending --preview --window previous_day → 注释校验 → review-start --claim`。没有上一自然日新增 `unread` 且没有到期 `keep` 时保持静默。有内容时只发送第一组卡片，等待用户决定后再继续；未配置时间时先发普通聊天询问，不要先执行同步，也不要生成“正在询问问题”但没有输入框的状态。

## 可分享结构

可分享包必须使用 `config.example.json`，不能携带个人 SQLite、runs、session、账号或 Vault 路径。运行时数据放到用户配置的数据目录，通过 `X_BOOKMARK_DIGEST_CONFIG` 或命令参数注入。不同 Agent 可以共享本 Skill 和核心 Python 脚本，但 X 登录态、输出通道、媒体渲染和 Obsidian 写入必须由适配层提供。详细拆分见 [references/portability.md](references/portability.md)。

## 文件边界

- `SKILL.md`：触发、主流程、动作和安全门槛。
- `references/annotation-contract.md`：注释字段和校验规则。
- `references/interaction-contract.md`：single/batch 卡片与回复协议。
- `references/state-and-safety.md`：状态、幂等、claim、远程修改和恢复。
- `references/portability.md`：安装、配置、跨 Agent 和分发。
- `references/modification-protocol.md`：试跑反馈的通用抽象、影响面分析和覆盖型回归协议。
- `scripts/`：确定性执行和诊断。
- `data/`、`runs/`：本地私有运行数据，禁止分发。
