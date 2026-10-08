# Storage

存储层负责当前 Flame 状态和执行历史的持久化，包括最近成功时间、连续状态、失败次数、奖励、下次动作以及待人工处理信息。

对上提供稳定存储接口，对下隔离数据库或文件实现；不得持久化明文凭据或不必要的认证页面内容。

SQLite schema v2 同一事务写入不可变 `keep_runs`、最新 `flame_state` 和每个账号最新的 `pending_notifications`；v1 自动事务迁移，保留状态与历史。投递成功后单独确认，失败持久化有限重试进度。执行锁避免同一数据库被两个 CLI runner 同时查询；status/health 仍可读。保存的 metadata 经过敏感键校验；Cookie、验证码图片和响应页面正文不属于持久化数据。

当前 schema v3 由 v1/v2 迁移，增加 action_attempts、runtime_pauses、login_requests。写动作必须先用 BEGIN IMMEDIATE 原子保留预算；pending 跨进程、跨日保留，确认当日不得再写。resume 不清动作记录。登录队列仅保存15分钟请求的标识/状态/时间，无账号凭据；控制台能查看最近20条记录。旧程序不保证兼容v3，回退不得重置领取历史。

登录请求与账号暂停同一事务写入。daemon 在打开窗口前先写 opening，若此处崩溃，重启只标记 interrupted，不再打开同一请求。runtime_commands 保存有15分钟期限的显式配置重载及完成/失败状态，不包含新配置正文。

schema v4 新增 `daemon_owner` / `run_requests`，由 v1/v2/v3 保留原记录升级。每个请求绑定 daemon 世代、配置指纹、Flame 和账号，UUID 原子去重；最多16个未完成请求，5分钟排队期限。只保存标识、固定原因、状态和 run_id，不保存浏览器信息或凭据。CLI 交接使用 existing_only，旧库/缺失库拒绝，不偷偷迁移。

新 daemon 在持有 execution_lock 及该 owner UUID 的专属 flock 后收拢旧请求；未开始的取消、已开始的标记 interrupted 并原子暂停对应 Flame，保留 action_attempts。正常退出同样收拢，浏览器关闭后才释放两把锁。不存在请求跨世代重放。入队需要当前 owner.active、通用 runner 锁及该 owner 的专属锁同时成立，防止普通 run-once 借通用锁冒充已崩溃的 daemon。

专属锁文件为数据库旁的 `.daemon-UUID.lock`，只接受规范 UUID，0600且内容为空，正常退出移除。进程异常退出后系统释放 flock；残留文件存在或另一个世代持锁均不算当前 owner 在线。active 和两种锁快照都不是认证有效性、执行进度或未来完成保证。此修订不改变 schema v4，不增加凭据、网络端口或系统安全配置。

动作确认必须显式提供原动作的 day，并按 keeper/account/task/day/pending 原子匹配，返回是否成功；确认不改写 day 或 started_at。错日、缺失或已确认记录不能被新观察覆盖。此修复不迁移 schema、不改既有记录，旧 pending 继续跨日阻止写操作。
