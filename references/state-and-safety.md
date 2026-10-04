# State and safety contract

## State model

- `backlog`：首次基线或暂不进入日常队列的历史条目。
- `unread`：新同步且尚未处理。
- `keep`：继续提醒；每个 digest 窗口只提醒一次。
- `read`：已处理，不再提醒；X 来源仍可保留。
- `removed`：全部本地来源已从 X 移除。

首次同步自动建立基线：当时已有且未被既有 session claim 的 `unread` 条目转为 `backlog`；既有 session 已 claim 的条目保留状态并刷新提醒时间。`last_digest_at` 只在成功 claim 或成功处理当前决定后更新。注释失败、队列校验失败、session 创建失败时不得消费提醒窗口。

旧版本已有条目但没有基线标记时，禁止在每日同步中猜测历史边界；应先做一次显式迁移并记录迁移时间。

每日窗口使用配置时区的上一自然日 `[since, until)`；默认按本地 `first_seen_at` 判断新增，同时补发仍为 `unread` 且从未 claim 的旧条目，避免某天失败后永久漏掉。`keep` 条目按提醒间隔单独判断，因此不会因为日历窗口切换而丢失待继续提醒的内容。需要滚动时间段时显式使用 `window=rolling`。

`sync.new` 只是首次出现的推文 ID 计数，不能当作 X 操作事件计数；已有推文新增来源时由 `latest_source_observed_at` 和 `new_source_memberships` 记录。接口没有真实操作时间时，空窗口只能报告“没有可确认的本地观察记录”，不能推断用户没有点赞或收藏。

## Queue identity

`review-start` 保存 `schema_version`、`queue_id`、账号、生成时间、时间窗口和 annotations 文件路径。`queue_id` 是 annotations 的稳定 SHA-256 指纹。使用 `card`、`review-apply` 或 `review-batch` 时，session 和 annotations 指纹不一致必须停止。

已有 session 默认不可覆盖；只有用户明确开始新一轮并传入 `--replace` 才能替换。旧 session 不得自动迁移到新队列。

## Idempotency

每次 `review-apply` 使用 operation ID。重复提交相同 operation ID 时返回已有结果，不重复写状态、不重复写 Obsidian、不重复调用 X。

`review-record` 是兼容旧流程的低层命令，不应作为新 Agent 的默认入口；新流程必须用 `review-apply` 或 `review-batch`。

同一个 session 的创建和提交使用旁路 `.lock` 文件串行化；Unix 下由 `fcntl` 提供跨进程锁。不同 session 仍应由宿主 Agent 避免重复 claim 同一批条目。

## External mutations

- 只有 `read` 会触发 `unbookmark`/`unlike`。
- `read` 必须传入 `--confirm READ:<tweet-id>`；自动化任务不得生成该确认。
- 来源按 `bookmark`、`like` 分别调用和记录。
- 某个来源成功、另一个来源失败时，立即保存成功来源的本地状态，只重试失败来源。
- 远程结果不确定时保持当前条目，报告需要人工核验，不标记为完成。

## Obsidian

- `defaults.obsidian_vault` 有值时写入 `<vault>/X Digest/<tweet-id>.md`。
- 未配置 Vault 时写入 `draft_dir/obsidian-pending/X Digest/<tweet-id>.md`，结果必须明确是“待导入”，不能声称已写入知识库。
- 写入成功后条目进入 `read`，但不从 X 移除。

## Recovery

发生崩溃后：

1. 读取 session 的最后一个成功 receipt；
2. 检查 `review_operations` 是否已有对应 operation；
3. 重试同一个 operation ID 以恢复 session 投影；
4. 不重新执行已完成的远程动作。
