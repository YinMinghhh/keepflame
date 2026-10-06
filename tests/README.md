# Tests

这里存放 KeepFlame 的自动化测试。测试集增长后可按 `unit/`、`integration/` 和 `fixtures/` 分层，并尽量与生产代码的架构边界对应。

默认测试不得依赖真实外部服务或个人账号；报告中应明确区分模拟测试、本地集成测试和真实服务验证。

当前测试使用假的 HTTP transport、Secret resolver、Clock 和 Keeper，并用临时 SQLite 验证事务与重启状态。测试 fixture 不包含真实 Cookie；真实聚宽复检必须单独执行和报告。
