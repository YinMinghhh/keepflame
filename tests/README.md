# Tests

这里存放 KeepFlame 的自动化测试。测试集增长后可按 `unit/`、`integration/` 和 `fixtures/` 分层，并尽量与生产代码的架构边界对应。

默认测试不得依赖真实外部服务或个人账号；报告中应明确区分模拟测试、本地集成测试和真实服务验证。

当前测试使用假的 HTTP transport、Secret resolver、Clock 和 Keeper，并用临时 SQLite 验证事务、迁移、投递重试和重启状态。conftest 默认阻断网络连接与测试临时目录外的凭据读取。`test_joinquant_flow.py` 从 CLI 贯穿人工签到闭环；`test_reliability.py` 覆盖晚间窗口、跨日/夏令时、退避、重复启动及通知交付边界。`test_email.py` 用注入的 SMTP 传输检查可选邮箱渠道：默认关闭、五种生命周期事件、重试预算、暂停期间重投、口令不落正文，以及 dry-run 不连接邮箱。`test_slack.py` 用注入的 Webhook 传输检查可选 Slack 渠道：默认关闭且不读文件不联网、地址与路径校验（拒绝非 hooks.slack.com、相对路径和仓库内路径）、消息脱敏与转义、1/2/4/8 分钟重试与 5 次上限、与邮箱互相独立且不重发、重载生效、超时范围；不连接真实 Slack。fixture 只有明确自造值；真实聚宽复检单独执行和报告。

`test_joinquant_auto.py` 检查自动流程仅一次提交、后台确认、登录失效、限流退避及账号目录边界；`test_joinquant_matching.py` 使用新生成图片验证复原/匹配和歧义拒绝，需要 browser 可选依赖。默认不会启动浏览器。

`KEEPFLAME_SYNTHETIC_BROWSER=1 python -m pytest tests/test_joinquant_browser.py` 单独启用真实 Chrome 合成验收。Chrome 从空临时会话启动，DNS 不解析外部主机，所有页面请求由 `route.fulfill` 合成；无测试端口、无真实凭据。它验证页面交互与本地算法相接，不证明当前聚宽线上可用。该测试另需本机 Google Chrome；不应自动下载或连接用户浏览器。

`test_joinquant_browser_guard.py` 是独立审查反例的回归：页面额外写请求/重复提交/其他积分规则、实际签到403/429、重定向、伪造fetch成功。测试用本地注入传输替代原生请求，保持真实Chrome执行页面与网络门控。`test_joinquant_guard.py` 单独检查生产原生通道的固定目标、禁止重定向/重试及请求预算。profile 链接测试只创建临时合成目录和 sentinel，不打开真实会话数据库。

`test_browser_session_handoff.py` 默认离线检查同一会话的一次性使用、失败后拒绝重试、账号/profile/origin 绑定和退出关闭。显式设置 `KEEPFLAME_SYNTHETIC_BROWSER=1` 后，其 Chrome 用例只连接操作系统分配端口的回环 TLS fixture，签发合成 HttpOnly/Secure 会话 Cookie，验证从登录页面交给 runtime 时无需关闭浏览器即可通过原生状态读取。该用例禁用外部 DNS、拒绝 Cookie/storage state 导出，结束后关闭本次创建的浏览器与本地服务；不连接聚宽或其他预览端口。

`test_runtime_control.py` 验证默认 dry-run 不初始化资源、原子动作意图、跨日/重启/恢复不重领、账号暂停、正式登录队列及同一上下文复用。显式 Chrome 测试只使用 fixture.invalid 的内存合成页面和空 profile，DNS 全拒绝，证明 HttpOnly 会话 Cookie 可连续供两个任务页使用且未导出。部署工具测试仅在临时合成 Git 仓库操作。

`test_review_runtime.py` / `test_review_reading.py` 保留独立复核发现的反例：登录窗口崩溃重开、请求/暂停竞态、popup和关闭失败时的门禁、document重定向及未知响应值进入SQLite。修复后这些安全性质必须持续成立。

`test_joinquant_request_diagnostics.py` 检查原文章 runList POST 只能被本地阻断一次，范围变化仍停止；首个致命请求的固定标签不泄漏未知 URL/参数/header/body，且经过 Keeper、CLI 和 JSONL 后仍保留。阅读 Chrome fixture 包含初始化 XHR 失败与未知写反例，确认前者不会阻止原页自然阅读，后者保留 pending 并停止。运行时回归另确认任务全部停用、idle 或借用者关闭 runtime 时上下文仍属于原 pool，由 owner 明确关闭。

`test_daemon_dispatch.py` 验证 CLI 到现有 daemon 的单次任务队列，包括独立客户端 Python 进程退出、并发 UUID 去重、容量/期限、配置/账号绑定、原有暂停/排期/退避、调度冲突、异常与信号退出、重启不重放，以及 v3→v4 保留历史和 pending。信号测试调用合成 handler，崩溃测试构造新库中的在途状态；不终止真实进程、不读取真实状态库。

`test_daemon_dispatch_browser.py` 另有两个 opt-in Chrome 场景：合成正式登录后，CLI 签到/阅读任务沿原上下文及未改动的聚宽门禁运行；阅读中的未知 POST 被拒绝并保留 pending。另一个主动关闭自己的合成 context，验证后续任务停止且不自动重建。只用空临时 profile、合成 HttpOnly Cookie、内存传输；不包含真实认证、跨日有效期、跨进程会话恢复或领取成功验收。应通过受限验证脚本禁用 DNS 并设置关闭的回环代理；不得为了用例通过放宽生产路由。

`test_daemon_owner_liveness.py` 覆盖独立复核的P2：合成daemon子进程os._exit后，普通run-once子进程持通用锁，第三个CLI客户端必须拒绝入队。`daemon_owner_process.py` 只允许新建测试目录内写入，拒绝浏览器、网络、真实账户文件，并为握手设置超时。另检查专属锁的世代绑定、非法owner拒绝、0600/空文件、崩溃残留不代表活性，以及浏览器关闭前两把锁均保留。正常交接、UUID幂等和重启不重放继续由既有定点测试覆盖。
