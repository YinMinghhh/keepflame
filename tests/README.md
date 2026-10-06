# Tests

这里存放 KeepFlame 的自动化测试。测试集增长后可按 `unit/`、`integration/` 和 `fixtures/` 分层，并尽量与生产代码的架构边界对应。

默认测试不得依赖真实外部服务或个人账号；报告中应明确区分模拟测试、本地集成测试和真实服务验证。

当前测试使用假的 HTTP transport、Secret resolver、Clock 和 Keeper，并用临时 SQLite 验证事务、迁移、投递重试和重启状态。conftest 默认阻断网络连接与测试临时目录外的凭据读取。`test_joinquant_flow.py` 从 CLI 贯穿人工签到闭环；`test_reliability.py` 覆盖晚间窗口、跨日/夏令时、退避、重复启动及通知交付边界。fixture 只有明确自造值；真实聚宽复检单独执行和报告。
