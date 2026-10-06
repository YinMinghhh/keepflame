# Core

核心层负责 Flame、Keeper、Context、结果状态、调度和插件发现等稳定契约。

这里不应出现具体平台的 URL、页面选择器、登录步骤或第三方 SDK 调用。核心只编排生命周期并消费统一结果，不统一各平台的执行过程。

当前核心提供统一结果/人工操作模型、严格 TOML 配置、entry-point Keeper 发现、事务状态转换以及每日检查窗口调度。`SchedulerService` 只消费配置和 `next_action_at`，不包含聚宽 URL 或 HTTP 规则。
