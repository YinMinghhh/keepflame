# Storage

存储层负责当前 Flame 状态和执行历史的持久化，包括最近成功时间、连续状态、失败次数、奖励、下次动作以及待人工处理信息。

对上提供稳定存储接口，对下隔离数据库或文件实现；不得持久化明文凭据或不必要的认证页面内容。

SQLite schema v2 同一事务写入不可变 `keep_runs`、最新 `flame_state` 和每个账号最新的 `pending_notifications`；v1 自动事务迁移，保留状态与历史。投递成功后单独确认，失败持久化有限重试进度。执行锁避免同一数据库被两个 CLI runner 同时查询；status/health 仍可读。保存的 metadata 经过敏感键校验；Cookie、验证码图片和响应页面正文不属于持久化数据。
