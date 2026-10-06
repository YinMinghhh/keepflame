# Tests

这里存放 KeepFlame 的自动化测试。测试集增长后可按 `unit/`、`integration/` 和 `fixtures/` 分层，并尽量与生产代码的架构边界对应。

默认测试不得依赖真实外部服务或个人账号；报告中应明确区分模拟测试、本地集成测试和真实服务验证。

当前测试使用假的 HTTP transport、Secret resolver、Clock 和 Keeper，并用临时 SQLite 验证事务、迁移、投递重试和重启状态。conftest 默认阻断网络连接与测试临时目录外的凭据读取。`test_joinquant_flow.py` 从 CLI 贯穿人工签到闭环；`test_reliability.py` 覆盖晚间窗口、跨日/夏令时、退避、重复启动及通知交付边界。fixture 只有明确自造值；真实聚宽复检单独执行和报告。

`test_joinquant_auto.py` 检查自动流程仅一次提交、后台确认、登录失效、限流退避及账号目录边界；`test_joinquant_matching.py` 使用新生成图片验证复原/匹配和歧义拒绝，需要 browser 可选依赖。默认不会启动浏览器。

`KEEPFLAME_SYNTHETIC_BROWSER=1 python -m pytest tests/test_joinquant_browser.py` 单独启用真实 Chrome 合成验收。Chrome 从空临时会话启动，DNS 不解析外部主机，所有页面请求由 `route.fulfill` 合成；无测试端口、无真实凭据。它验证页面交互与本地算法相接，不证明当前聚宽线上可用。该测试另需本机 Google Chrome；不应自动下载或连接用户浏览器。

`test_joinquant_browser_guard.py` 是独立审查反例的回归：页面额外写请求/重复提交/其他积分规则、实际签到403/429、重定向、伪造fetch成功。测试用本地注入传输替代原生请求，保持真实Chrome执行页面与网络门控。`test_joinquant_guard.py` 单独检查生产原生通道的固定目标、禁止重定向/重试及请求预算。profile 链接测试只创建临时合成目录和 sentinel，不打开真实会话数据库。
