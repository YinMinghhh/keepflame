# Core

核心层负责 Flame、Keeper、Context、结果状态、调度和插件发现等稳定契约。

这里不应出现具体平台的 URL、页面选择器、登录步骤或第三方 SDK 调用。核心只编排生命周期并消费统一结果，不统一各平台的执行过程。

当前核心提供统一结果/人工操作模型、严格 TOML 配置、entry-point Keeper 发现、事务状态转换和每日检查窗口。跨日 `next_action_at` 与插件冷却下限会保留，失败按有上限的指数退避复检。`SchedulerService` 分别安排远端检查与待通知投递；通知失败不会触发额外 Keeper 查询，重试预算不因重启而重置。核心不包含聚宽 URL 或 HTTP 规则。

`automation_paused` 是独立于 WAITING_HUMAN 的持久闸门，`pause_account` 可暂停同 Keeper 的所有账号任务。调度器跳过已暂停任务，继续独立处理已有通知。动作意图由 Storage 端口支持；Core 不解析平台任务规则。重复 keeper/account/task 配置被拒绝，省略 enabled 默认为关闭。
