# KeepFlame

KeepFlame 维护长期存在的外部状态。第一条流程是聚宽每日签到：查询官方账号状态 → 提醒用户在官方页面完成签到 → 复检确认成功 → 次日继续。需要滑块、登录更新或其他身份验证时进入 `WAITING_HUMAN`；不会自动求解验证码，也不调用积分领取接口。

## 开发与离线验证

要求 Python 3.13+；新环境可用 `uv sync --dev` 安装。已有 `.venv` 可以直接验证：

```bash
.venv/bin/python -m pytest
.venv/bin/python -m pytest tests/test_joinquant_flow.py tests/test_reliability.py
.venv/bin/keepflame --help
```

默认测试拦截网络连接，只允许读取测试临时目录内自造的凭据。CLI 合成验收覆盖未签到、首次通知、晚间提醒、人工完成后的复检、重启、次日身份失效；全部使用 Fake HTTP，不接触真实账号。

## 聚宽运行入口

只有在另行确认真实账号接入后，准备个人运行配置和仓库外 Cookie：

```bash
cp config/keepflame.local.example.toml config/keepflame.local.toml
# 编辑 cookie_file，指向仓库外的绝对路径；不要把 Cookie 写入 TOML。
.venv/bin/keepflame health --config config/keepflame.local.toml
.venv/bin/keepflame status --config config/keepflame.local.toml
.venv/bin/keepflame run-once --config config/keepflame.local.toml --flame joinquant-primary
# 准备好持续检查时，在前台运行；Ctrl-C / SIGTERM 退出。
.venv/bin/keepflame run --config config/keepflame.local.toml
```

`run-once` 是显式即时复检，不受每日窗口或已成功状态限制；退出码：成功 `0`、等待人工 `2`、失败 `1`。它输出结果并持久化待通知事件，后续 `run` 会发送。`run` 按计划查询，同一天成功后不再查；同一数据库只允许一个 `run` 或 `run-once`，重复启动会明确报错。常驻运行期间，人工签到后等待下一次复检；若必须即时检查，先停止常驻实例再用 `run-once`。

`status --json` 同时显示状态和通知的待发送/重试耗尽情况。`health` 只验证配置、插件装载和 SQLite；不会查询聚宽，不代表账号已签到或提醒渠道健康。`status/health` 会初始化配置所指的数据库。

Cookie 文件应是单行原始请求头值，无 `Cookie:` 前缀和末尾换行；建议仅服务账号可读。每次查询重新读取，因此轮换不需重启。不同账号使用独立 `account_id` 和独立文件。真实配置、凭据和运行库均不提交。

## 调度与通知

示例使用上海时区，检查窗口为 `[09:00, 23:00)`，等待人工时每小时复检，20:00 后首次复检发一次晚间提醒。关闭窗口后保留次日计划；停机错过的检查恢复后只补查一次。失败按 1、2、4、最多 6 小时退避（以配置的复检间隔起步），遵守每日窗口和服务端更晚的 `Retry-After`，不会忙循环。单次 HTTP 查询超时 15 秒，网络或安全网关错误最多尝试两次；429 或有有效 `Retry-After` 的网关响应不立即重试。

当前渠道仍是 stdout JSONL，尚未连接 Telegram 或邮箱。`Notifier` 是可替换接口，`build_runtime(..., delivery_notifier=...)` 可注入具体渠道；运行事件日志与投递接口分开。状态、历史、最新待通知事件同事务保存；旧的人工待办在恢复后会被恢复事件替换。

投递失败只保存错误类型；每个事件最多尝试 5 次，等待 1、2、4、8 分钟，重启会保留进度。通知重试独立于聚宽查询，窗口关闭也不触发额外查询。耗尽后待通知状态保留并在 `status` 中可见；修好渠道后，需要产生新的状态事件才能启动新一轮自动尝试。发送成功才确认提醒时间。交付采用至少一次语义：进程若在发送成功、确认写库前退出，可能重发；稳定 `notification_id` 可供渠道去重。

## Docker Compose

```bash
export KEEPFLAME_CONFIG_FILE=/absolute/path/to/keepflame.local.toml
export JOINQUANT_COOKIE_FILE=/absolute/path/to/joinquant.cookie
docker compose up --build -d
docker compose logs -f keepflame
```

非 root、只读根文件系统，不开放端口；配置和 Cookie 只读挂载，SQLite 使用独立数据卷。schema v1 会事务升级为 v2，保留已有状态与历史并增加待通知表。容器构建/健康检查不能替代真实账号验收。

真实验收需单独确认最小步骤：用户准备仓库外 Cookie；核对未签到映射为 `WAITING_HUMAN`；确认通知到达；用户在官方页面完成操作；确认复检为 `SUCCESS`。真实账号、收件人、外部通知和常驻部署不属于离线测试。
