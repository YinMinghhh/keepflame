# Storage

存储层负责当前 Flame 状态和执行历史的持久化，包括最近成功时间、连续状态、失败次数、奖励、下次动作以及待人工处理信息。

对上提供稳定存储接口，对下隔离数据库或文件实现；不得持久化明文凭据或不必要的认证页面内容。

SQLite schema v2 同一事务写入不可变 `keep_runs`、最新 `flame_state` 和每个账号最新的 `pending_notifications`；v1 自动事务迁移，保留状态与历史。投递成功后单独确认，失败持久化有限重试进度。执行锁避免同一数据库被两个 CLI runner 同时查询；status/health 仍可读。保存的 metadata 经过敏感键校验；Cookie、验证码图片和响应页面正文不属于持久化数据。

当前 schema v3 由 v1/v2 迁移，增加 action_attempts、runtime_pauses、login_requests。写动作必须先用 BEGIN IMMEDIATE 原子保留预算；pending 跨进程、跨日保留，确认当日不得再写。resume 不清动作记录。登录队列仅保存15分钟请求的标识/状态/时间，无账号凭据；控制台能查看最近20条记录。旧程序不保证兼容v3，回退不得重置领取历史。

登录请求与账号暂停同一事务写入。daemon 在打开窗口前先写 opening，若此处崩溃，重启只标记 interrupted，不再打开同一请求。runtime_commands 保存有15分钟期限的显式配置重载及完成/失败状态，不包含新配置正文。

schema v4 新增 `daemon_owner` / `run_requests`，由 v1/v2/v3 保留原记录升级。每个请求绑定 daemon 世代、配置指纹、Flame 和账号，UUID 原子去重；最多16个未完成请求，5分钟排队期限。只保存标识、固定原因、状态和 run_id，不保存浏览器信息或凭据。CLI 交接使用 existing_only，旧库/缺失库拒绝，不偷偷迁移。

新 daemon 在持有 execution_lock 及该 owner UUID 的专属 flock 后收拢旧请求；未开始的取消、已开始的标记 interrupted 并原子暂停对应 Flame，保留 action_attempts。正常退出同样收拢，浏览器关闭后才释放两把锁。不存在请求跨世代重放。入队需要当前 owner.active、通用 runner 锁及该 owner 的专属锁同时成立，防止普通 run-once 借通用锁冒充已崩溃的 daemon。

专属锁文件为数据库旁的 `.daemon-UUID.lock`，只接受规范 UUID，0600且内容为空，正常退出移除。进程异常退出后系统释放 flock；残留文件存在或另一个世代持锁均不算当前 owner 在线。active 和两种锁快照都不是认证有效性、执行进度或未来完成保证。此修订不改变 schema v4，不增加凭据、网络端口或系统安全配置。

动作确认必须显式提供原动作的 day，并按 keeper/account/task/day/pending 原子匹配，返回是否成功；确认不改写 day 或 started_at。错日、缺失或已确认记录不能被新观察覆盖。此修复不迁移 schema、不改既有记录。关联 keep_runs 里 stage_counts.claim 恰好为整数 0、且没有归档的 pending，可以在更晚的业务日重新预留；当前行换成新的 pending，历史不删除。已提交领取、同一天或缺少该计数的 pending 仍阻止写入。已有 unknown 或其他归档的 pending 不因零次领取解除 Flame 暂停。账号范围的暂停不因零次领取自动解除。

schema v5 仅添加动作审计表和触发器，支持从 v1/v2/v3/v4 升级；迁移逐字段复制现有 action_attempts 到 action_history，不更改原行。以后每次保留预算或确认都追加完整快照，历史与 action_archives 禁止 UPDATE/DELETE，当前意图也禁止 DELETE。新日预留可替换当前槽位，原始 pending、日期和开始时间仍永久保存在历史中。

显式归档只接受精确的 keeper/account/task/day/started_at 身份和已过期业务日。unknown 不释放预算；abandoned 必须通过 Keeper 对同一已落库 run 的离线证据审查。归档追加 source revision、证据SHA256、原run完整快照、原暂停快照，不改变原动作行、不写成功、不删除暂停。get_action 对已审查 abandoned 返回终止投影，原始数据库行仍为 pending；只允许晚于旧日且不早于归档业务日的新预算。归档后的原意图不能再 confirm；稳定 UUID 重试幂等，变更证据/身份使用同 UUID 会拒绝。

时间校验回调在预留/确认事务取得写锁后和更新后各执行一次；日期变化会回滚动作与快照，避免等锁或写入期间换日后错误确认。回调是纯本地时间检查，不做网络。CLI archive-action 使用 existing_only，不偷偷升级旧库，不构建 Runtime 或浏览器。先完成受控迁移，再使用新客户端；旧版本不能直接操作 v5。该变更不把结果未知的写入变成可重试。

schema v6 为 run/state 增加可空的业务目标与完成回执 JSON，为 run 增加显式墙钟回退标记，为 daemon 请求增加目标日期/时区。从 v1–v5 事务迁移，只加列，不据历史 finished_at 推测成功日，也不重写任何既存业务字段；旧回执/目标保持 NULL。旧客户端应拒绝 v6，回退不能覆盖历史库。

新请求入队即固定配置时区的业务日；消费时与 Keeper 入口再核对，过日或缺失目标的旧请求拒绝，UUID 重试仍返回原请求，不转成新日预算。keep_runs、最新状态及通知在同一事务保留相应回执；通知重试不改变原事件业务日。真实库须在停用状态下备份并单独批准迁移，本开发测试没有打开真实库。
