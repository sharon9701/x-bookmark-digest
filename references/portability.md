# Portability and distribution

## Package versus runtime data

可分发包只包含规则、脚本、references、测试和 `config.example.json`。以下内容必须留在用户运行目录：

- SQLite 状态库；
- annotations、pending、session、receipt 和 Markdown 草稿；
- X 账号名、浏览器登录态、Cookie、Token；
- Obsidian Vault 路径。

通过 `X_BOOKMARK_DIGEST_CONFIG` 指向用户自己的配置；也可以用 `--db` 覆盖状态库路径。不要把个人配置复制进 Skill 包。

## Runtime requirements

最低依赖：

- Python 3.10+；
- 标准库 `sqlite3`、`zoneinfo`；
- `opencli` 及可用的 `twitter bookmarks`、`twitter likes`、`twitter whoami` 命令；
- 已授权的当前 Agent 浏览器或 X 连接器。

先运行：

```bash
python3 scripts/doctor.py
python3 scripts/doctor.py --skip-auth
```

`doctor` 还会检查分类名称是否唯一，并报告 session 文件锁是否可用。Unix 环境使用 `fcntl` 锁；不支持文件锁的平台仍可离线渲染，但并发写同一个 session 时应由宿主 Agent 保证串行。

如果 Agent 没有浏览器或 X 连接器，只能处理已有 pending/annotations 文件，不能执行 `sync` 或远程移除。

当前 X 读取接口通常只给出列表顺序和推文发布时间，不给出加入书签或点喜欢的事件时间。默认用本地 `first_seen_at` 作为新增代理；要保证跨天补跑仍按真实事件归属，连接器应额外提供 `source_added_at`，核心层再将其作为窗口字段。

## Agent adapter boundary

可共享的核心是：

- annotations schema；
- 状态和 receipt 规则；
- batch reply parser；
- Markdown 卡片渲染；
- SQLite 操作和离线测试。

需要按 Agent 适配的是：

- X 数据读取和登录态；
- 消息发送通道；
- 图片、视频和文件预览能力；
- Obsidian 写入能力；
- 远程写操作审批。

媒体兼容性：不同 Agent 宿主对远程图片、跨域和 X CDN 的支持不同。`defaults.inline_media_previews` 默认为 `false`，因此分享包应使用文字提示和原帖链接作为稳定回退；只有目标宿主已经验证可加载 `pbs.twimg.com` 时才由用户显式开启。

建议将连接器实现为 `fetch_sources()`、`remove_source()`、`whoami()` 三个窄接口，核心逻辑不要直接依赖某个 Agent 的 UI。

## Sharing forms

1. **项目内 Skill**：放到项目的 `.codex/skills/x-bookmark-digest/`，适合团队共享。
2. **个人 Skill**：安装到用户的 `~/.codex/skills/x-bookmark-digest/`，只影响本机。
3. **插件**：在外层加入 `.codex-plugin/plugin.json`，适合向多个 ChatGPT/Codex 环境分发。
4. **跨产品**：保留 Agent Skills 标准目录和 provider-neutral 文案，再为 Claude、Cursor 等产品补自己的安装和连接器说明。

不要把“当前浏览器已有 X 登录态”写成可分享 Skill 的默认前提，也不要把本人的账号、分类偏好和 Vault 路径写死在 description 中。
