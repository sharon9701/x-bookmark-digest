# X Bookmarks + Likes Digest

这个 Skill 将**已授权 X 账号的 Bookmarks（书签）和 Likes（喜欢/点赞）**变成可恢复的本地阅读队列和聊天卡片。它只读取这两个栏目，不读取时间线、搜索或公开账号收藏，也不会自动发布、点赞、收藏或回复。

每日模式默认检查配置时区下的**上一自然日**。首次同步会建立历史基线，已有内容进入 `backlog`，不会把全部历史收藏一次性推送；后续只处理新发现的条目和到期的 `keep` 条目。X 接口没有可靠的“加入书签/点喜欢时间”时，本地首次观察时间只是代理，不能据此断言用户实际操作时间。

## 安装

把 `x-bookmark-digest/` 放入 Codex 的 skills 目录（通常是 `~/.codex/skills/`），或在项目中通过 `$x-bookmark-digest` 显式调用。安装 Skill **不会自动创建定时任务**；如果需要每日主动提醒，还要在 Codex 中单独创建并启用一个定时任务。

运行环境需要：

- Python 3.11 或更高版本；
- 已安装并登录的 `opencli`，且支持 `twitter bookmarks` 和 `twitter likes`；
- 用户对 X 账号有合法访问权限；
- 若使用自动化，本机保持开机，Codex 桌面应用保持运行，X 登录态有效。

## 初次配置

复制示例配置并按目标账号修改分类、时区和路径：

```bash
cp config.example.json config.json
python3 scripts/doctor.py
```

也可以通过环境变量指定配置文件：

```bash
export X_BOOKMARK_DIGEST_CONFIG=/path/to/config.json
```

`config.json`、SQLite 数据库、session、X 登录态和运行记录都是本地私有数据，不要放进分享包或提交到公共仓库。

## 手动运行一次

先检查环境并同步 Bookmarks + Likes：

```bash
python3 scripts/doctor.py
python3 scripts/x_bookmark_digest.py sync --limit 200
python3 scripts/x_bookmark_digest.py pending --preview --window previous_day \
  --output runs/pending.json
```

随后由 Agent 按 [注释契约](references/annotation-contract.md) 为 `pending.json` 生成 `runs/annotations.json`，校验并冻结本轮队列：

```bash
python3 scripts/x_bookmark_digest.py review-start \
  --annotations runs/annotations.json \
  --session runs/review-session.json --claim
```

手动试跑可以直接说“现在推送”或“试跑一次”；不需要先设置长期推送时间。

## 聊天卡片和动作

默认一次只推送 1 张卡片。用户只回复当前卡片的动作，处理完成后再推送下一条，不需要选择第几条。卡片保留原贴标题、作者、来源、原帖链接、媒体提示、摘要和要点；默认不内嵌 X 远程图片，避免聊天宿主出现破图占位符。图片仍保留数量提示和打开链接，回复协议见 [交互契约](references/interaction-contract.md)。如目标宿主确认支持 X CDN，可在 `defaults.inline_media_previews` 设为 `true` 后试用内嵌预览。

```bash
python3 scripts/x_bookmark_digest.py card \
  --annotations runs/annotations.json \
  --session runs/review-session.json
```

用户直接回复动作：

```text
2
```

四个动作固定为：

1. `1`：`read`，移出当前存在的书签/喜欢来源；
2. `2`：`keep`，保留至下次提醒；
3. `3`：纳入 Obsidian；未配置 Vault 时生成待导入 Markdown；
4. `4 分类名`：调整分类。

处理成功后继续调用 `card --session` 推送下一条。只有用户明确要求批量处理时，才使用 `cards`、多行回复和 `review-batch`。状态、幂等、失败恢复和远程移除规则见 [状态与安全](references/state-and-safety.md)。

## 定时推送

配置文件中的 `defaults.schedule` 只保存 Skill 的时间偏好，例如：

```json
"schedule": {"time": "19:00", "days": "daily"}
```

真正的后台执行仍需在 Codex 中创建定时任务，并在任务提示中调用 `$x-bookmark-digest`。任务应明确时区、上一自然日窗口、无内容时静默，以及不要续推已完成旧队列。时间只决定何时运行，不改变内容窗口。

## 分享和打包

可分享内容包括：

```text
SKILL.md
README.md
agents/openai.yaml
config.example.json
references/
scripts/
tests/
```

不要分享：`config.json`、`data/`、`runs/`、SQLite、session、缓存、个人账号信息或登录凭据。跨 Agent 的适配要求见 [可移植性](references/portability.md)。

可以用以下命令验证并打包：

```bash
python3 ~/.codex/skills/.system/skill-creator/scripts/quick_validate.py .
zip -r ../x-bookmark-digest.zip . \
  -x 'config.json' 'data/*' 'runs/*' \
     'scripts/__pycache__/*' 'tests/__pycache__/*' '*.pyc' '.DS_Store'
```
