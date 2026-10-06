# Storage

存储层负责当前 Flame 状态和执行历史的持久化，包括最近成功时间、连续状态、失败次数、奖励、下次动作以及待人工处理信息。

对上提供稳定存储接口，对下隔离数据库或文件实现；不得持久化明文凭据或不必要的认证页面内容。

SQLite schema v2 同一事务写入不可变 `keep_runs`、最新 `flame_state` 和每个账号最新的 `pending_notifications`；v1 自动事务迁移，保留状态与历史。投递成功后单独确认，失败持久化有限重试进度。执行锁避免同一数据库被两个 CLI runner 同时查询；status/health 仍可读。保存的 metadata 经过敏感键校验；Cookie、验证码图片和响应页面正文不属于持久化数据。

当前 schema v3 由 v1/v2 迁移，增加 action_attempts、runtime_pauses、login_requests。写动作必须先用 BEGIN IMMEDIATE 原子保留预算；pending 跨进程、跨日保留，确认当日不得再写。resume 不清动作记录。登录队列仅保存15分钟请求的标识/状态/时间，无账号凭据；控制台能查看最近20条记录。旧程序不保证兼容v3，回退不得重置领取历史。
