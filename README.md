# KeepFlame

KeepFlame 为不同网站的日常任务保留统一的调度、状态、人工介入和历史。聚宽插件有 `inspect`（只读查询）和 `browser`（专用浏览器）模式；签到 `task="checkin"` 与阅读积分 `task="reading"` 是两个独立 Flame，共用账号会话。

**默认不执行。** `run`、`run-once`、`login`、`resume`、`reload` 不带 `--execute` 时只校验配置并输出计划，不开数据库、浏览器或凭据文件；省略 `enabled` 也默认为关闭。所有发布样例停用，邮件功能本轮暂停，现有通知仅写 stdout JSONL。

## 已验证到哪一层

- 文档目标：每日维护多个服务。目前只有聚宽适配器，Telegram/游戏尚未实现。
- 源码：严格状态解析、自然打开公开文章、单次领取门控、SQLite 动作意图、持久暂停、常驻浏览器上下文，以及默认停用的 Mac 部署准备工具。
- 离线验证：假的 HTTP、合成图片/页面、临时 SQLite 与空白 Chrome profile；真实账号不参与开发验证。
- 既有真实证据：2026-10-06 16:54（上海时间）曾对获准账号完成一次签到，后台状态由缺失变为整数 `1`。这只证明当时那一次签到；新增阅读积分、长期运行和服务器部署尚未做真实验收。

## 开发与测试

Python 3.13+。新环境安装命令仅供准备环境，已有依赖无需重新安装：

```bash
uv sync --locked --extra browser --dev
.venv/bin/python -m pytest
KEEPFLAME_SYNTHETIC_BROWSER=1 .venv/bin/python -m pytest tests/test_runtime_control.py tests/test_joinquant_browser.py tests/test_joinquant_reading_browser.py
```

默认测试阻断外网和临时目录外的凭据读取；显式 Chrome 测试只用空白合成 profile、本地生成页面及受限传输。它们不证明线上网页完全兼容。浏览器可选依赖为 Playwright、Pillow、NumPy，还需已安装的 Google Chrome。

## 运行计划与账号状态

复制 [浏览器配置样例](config/keepflame.browser.example.toml)，真实配置、状态库和专用 profile 均放仓库外。独立阅读配置见 [服务器样例](deploy/keepflame.server.toml.example)。先保留全部任务关闭：

```bash
keepflame run --config /path/to/keepflame.toml
keepflame status --config /path/to/keepflame.toml --json
keepflame health --config /path/to/keepflame.toml
```

第一条是无副作用计划。`status`/`health` 会初始化本地 SQLite；`remote_checked=false` 表示它们不会验证聚宽登录或任务完成，不能作为真实运行健康证明。`status` 包含持久暂停、通知待办和登录请求状态。

后续获准使用真实账号后，才编辑任务开关并添加 `--execute`。同一数据库仅允许一个执行器；独立的 `run-once --execute` 跳过时间窗口，但尊重持久暂停和未确认动作记录，不能用它循环重试。需要保留登录上下文时使用下述显式交接入口。

## 登录只交给同一个常驻进程

浏览器上下文由 `run --execute` 持有，任务页每轮关闭；下次任务仍使用同一上下文。KeepFlame 不导出 Cookie、不连接日常浏览器，也不保证 Chrome 退出后会话仍有效。

任务全部停用、暂停或等待下一次排期时，daemon 保留上下文且不发送页面保活请求；15 分钟限制只属于人工登录窗口。配置 reload 不重启浏览器。没有 `--via-daemon` 的 `run-once` 仍会在结束时关闭自有上下文，不应通过每天重启它维持登录。进程退出后的认证结果也不能推断网站要求每天重新登录。当前不启用 Chrome 会话恢复开关：恢复旧页面可能在任务门禁安装前联网，必须另行验证启动前的请求隔离。

以下是未来获准接入的操作流程，本轮不需要用户登录：

```bash
# GUI 登录用户内启动；任务可先全部保持 disabled。
keepflame run --config /path/to/keepflame.toml --execute
# 另一个终端请求同一 daemon 打开专用登录窗口。
keepflame login --config /path/to/keepflame.toml --flame joinquant-primary --execute
keepflame status --config /path/to/keepflame.toml --json
# 等状态为 waiting，在官方窗口人工登录；REQUEST_ID 来自前一步输出。
keepflame login --config /path/to/keepflame.toml --confirm REQUEST_ID --execute
```

完成登录后，可以编辑同一个配置文件启用已获准的任务，再执行 `keepflame reload --config /path/to/keepflame.toml --execute`。daemon 在当前任务结束后应用配置，保留浏览器上下文和未决动作；无需重启。`status --json` 的 `runtime_commands` 必须显示 completed 才表示已应用。重载不支持更换数据库；无效配置会停止派发任务，修正后需再次显式重载。状态里的配置开关来自当前文件，不代表 daemon 已经重载。

请求只存本地 SQLite，daemon 每至多 5 秒处理一次；正在执行的有界任务结束后才处理新请求。窗口最多 15 分钟，登录阶段禁止签到、阅读登记和领取接口。确认仅表示用户完成操作，后续独立状态查询仍需证明登录有效。取消用 `--cancel REQUEST_ID --execute`；超时、关闭窗口或进程重启不会自动重新登录。服务器必须独立登录，不复制开发机 profile。

## 将单次任务交给已有 daemon

`run-once --via-daemon` 通过现有本地 SQLite 提交一个已启用 Flame；客户端不构建 Runtime，不打开浏览器或凭据，也不在 daemon 缺失时改为独立执行。签到与阅读各提交自己的 Flame ID。以下命令只输出计划；接入实际账户与执行任务仍需相应授权：

```bash
keepflame run-once --via-daemon --config /path/to/keepflame.toml \
  --flame joinquant-primary --request-id 72ec11c8-68a0-4a32-99ee-ed105d24d3cf
```

新任务使用新 UUID；重交同一任务必须保留原 UUID。加 `--execute` 才入队，退出码 0 仅表示请求已提交或找到原记录。`status --json` 的 `daemon_dispatch.requests` 保存最近 20 条状态：`completed` 表示已完成一次 Keeper 运行，业务结果见 `result_status`、`run_id` 及 Flame 状态；`FAILED`/`WAITING_HUMAN` 不能当作成功。相同 UUID 永不重新执行，不同 UUID 的同一未完成 Flame 也会拒绝。

队列最多 16 个未完成请求；等待期限 5 分钟，每轮最多消费一个。请求绑定本次 daemon、配置路径/指纹、Keeper、账号及 Flame；入队还需核对当前 owner UUID 专属的进程锁。普通 run-once 持有通用 runner 锁不能证明 daemon 存活。执行时仍检查启用、配置重载、账号/任务暂停、登录窗口、每日窗口与退避。排队 Flame 暂由请求队列接手，防止同一调度轮再执行。daemon 的其他正常定时任务仍按配置运行。页面路由、403/429 处理和动作 pending 门禁不变，交接不是强制执行或重试入口。

daemon 正常退出取消未开始请求；已开始而未确认完成的请求标记 `interrupted` 并暂停对应 Flame。崩溃后的新 daemon 也只收拢这些状态，绝不自动重放旧请求或清除 action pending。通用执行锁和 owner 专属锁都保留到自有浏览器关闭后才释放。专属锁采用数据库旁的空白0600文件，正常关闭时删除；崩溃残留的空文件不证明活性，不扫描清理别的世代。状态中的 owner 是持久记录，`runner_lock_held`、`daemon_owner_lock_held` 是不同的锁快照；均不证明远端登录有效，也不能保证入队之后进程不会崩溃。不同客户端进程可以退出而不关闭 daemon 的原上下文；浏览器或 daemon 自身退出后没有会话恢复能力。上述合成验证不等于真实跨日、睡眠或重启后的认证验证。

## 调度、暂停与不确定结果

示例为 `Asia/Shanghai`，每日窗口 `[09:00, 23:00)`；签到与阅读时间错开。同日成功后不再自动查询，停机错过的检查只补查一次，不批量补领历史任务。一般传输失败按 1、2、4、最多 6 小时退避并尊重更晚的 `Retry-After`。

登录失效会持久暂停该 Keeper 的整个账号；挑战、协议变化和不确定领取暂停对应任务，后台不反复尝试。人工处理后 `resume --flame ID --execute` 只解除该任务暂停；浏览器账号登录暂停使用上面的登录确认流程。查询模式在人工更新原凭据文件后，可用 `resume --flame ID --account --execute` 明确恢复该 Keeper 的账号检查，后续仍需验证认证；此命令同样不清未决动作。

每次可能产生阅读资格或领取前，SQLite 原子记录 `(keeper, account_id, task)` 的动作意图。进程若在远端执行后崩溃，重启仍保留 `pending`，只能检查是否已完成；跨日、改 Flame ID 或 `resume` 都不会重新获得写入预算。后台明确完成才确认动作，同一确认日仍禁止新写。未确认记录不会自动删除；无法证明完成时保留待办，须另行人工核实，不能直接清库重试。

SIGTERM/Ctrl-C 停止领取新任务，等待本轮有界流程完成并关闭自有上下文。强制终止无清理保证，但已落盘意图仍阻止盲目重试。验证码通过不等于积分到账，只有后台明确完成才记录 `SUCCESS`；没有可靠奖励数量时不虚构积分。

## 部署与数据边界

[部署准备工具](deploy/README.md) 可冻结受审 Git 提交、生成校验包、默认停用的 LaunchAgent/配置，并做只读路径预检。服务器当前未连接、未安装、未启动；GUI 会话、睡眠/唤醒、依赖、网络和真实阅读都仍待服务器阶段核验。旧 Docker 查询样例不包含浏览器环境，默认命令也只会 dry-run。

SQLite schema v4 从 v1/v2/v3 事务迁移，保留动作、暂停、登录请求和执行历史，新增 daemon owner 与单次运行请求。交接客户端只接受已存在的 v4 库，不创建或迁移数据库；旧服务必须在后续获准升级时采用新代码。本轮开发只迁移新建合成库。回退源码前先核对数据库兼容性；不要恢复旧状态快照后直接重新领取。浏览器凭据与图片均不打包。挑战图片只在内存中处理，不写截图/HAR/视频；持久 profile 的其他浏览器缓存并未承诺完全为空。

通知保持现有 stdout JSONL/有界 outbox：单事件最多 5 次投递，间隔 1/2/4/8 分钟，错误只记类型；这不是邮件投递。LaunchAgent 输出日志不会自行轮转，启用服务器前需落实文档中的保留方案。

更多细节见 [聚宽接入](src/keepflame/flames/joinquant/README.md) 和 [请求协议及预算](src/keepflame/flames/joinquant/PROTOCOL.md)。本机技术验证不代表平台许可，真实操作范围仍需独立确认。

配置文件内容改变或不可读时，daemon 会在本轮结束后的下一调度检查停止派发旧任务；即使修回原文件，也要显式合法 reload 才解除暂停。reload CLI 只打开已存在且兼容的状态库，不会因候选配置改了路径就新建库。数据库路径变更不受支持：修回原路径后再发请求并检查 completed。
