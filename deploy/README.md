# Mac 服务器部署准备

`prepare.py` 只准备和检查文件。默认不写文件；显式 `--write --out` 也只创建一个新的仓库外产物目录，不安装 LaunchAgent、不改运行配置、不启动 KeepFlame/Chrome、不连接服务器。工具只用 Python 标准库；制作发布包另需本地 Git。

开发机承担源码、合成测试和冻结发布；服务器是唯一生产执行端。服务器离线时可以完成发布包与候选配置审查，不能据此认定它已具备 GUI、登录状态、依赖或正常网络出口。时间和调度统一使用 `Asia/Shanghai`。邮件本轮暂停，候选配置只使用现有本地日志与待人工状态。

## 1. 冻结源码，不同步开发环境

在最终测试和审查后，用**完整 40 位 HEAD SHA**制作发布包。工具要求当前 HEAD 与该 SHA 相同，工作树的 tracked/untracked 文件均干净，并在读取对象后再次检查；忽略的账号文件不会打包，也不会打开。以下命令仅示范，`RELEASE_COMMIT` 必须替换为本次受审提交，不能直接发布旧的 `2a478aa` 并假定它已有新版本的 dry-run 行为。

```sh
python3.13 -I -B deploy/prepare.py release --repo "$PWD" --commit "$RELEASE_COMMIT"
python3.13 -I -B deploy/prepare.py release --repo "$PWD" --commit "$RELEASE_COMMIT" \
  --write --out /absolute/review-area/new-release-bundle
python3.13 -I -B deploy/prepare.py verify --bundle /absolute/review-area/new-release-bundle
```

产物为确定性的 `keepflame-<commit>.tar`、`release.json` 和 `SHA256SUMS`。清单记录 commit/tree、归档 SHA-256、每个源文件的 SHA-256/大小；校验不解压、不导入产品模块。传输之后在服务器重新校验，并通过受信交付渠道比对**事先记录**的 SHA-256；相互匹配的包和清单只证明完整性，不独立证明发布者身份。

打包从 Git 对象读取，不递归复制工作目录。允许根级构建/锁文件和说明，`src`/`tests` 下 Python 与 Markdown，`config` 中命名明确的示例，以及本目录固定的准备工具/样例。任何新增未审核类型先拒绝，不能为了过检把真实网页或账号材料改名为源码。`.git`、链接/子模块、profile、运行状态/日志/备份、密钥文件、虚拟环境、未知配置及二进制都不分发。简单私钥标识检查是额外防线；源码审查仍须确保没有把凭据写入允许的文本文件。

**绝不搬运**开发机的运行配置、SQLite、Cookie、密码、profile、`.venv` 或调试证据；不需要 Git remote/push、SSH 密钥或新建公网访问。

## 2. 生成默认停用的候选文件

```sh
python3.13 -I -B deploy/prepare.py plan \
  --server-home /Users/keepflame --commit "$RELEASE_COMMIT"
python3.13 -I -B deploy/prepare.py plan \
  --server-home /Users/keepflame --commit "$RELEASE_COMMIT" \
  --write --out /absolute/review-area/new-server-plan
```

`/Users/keepflame` 只是待确认的运行用户示例。生成操作不会访问这个 home，也不会建运行目录；不能把 `--out` 指向安装、运行或既有证据目录。配置包含两个独立 Flame，签到保留 `joinquant-primary`，阅读为 `joinquant-reading-primary`，均 `enabled=false`，使用同一服务器自有账号 profile。阅读的 `article_url` 留空，需审查具体公开文章后再配置；不会自动挑文章或假定停留 6 秒即可领取。

目录分工：

| 目录 | 职责 |
| --- | --- |
| `~/Applications/KeepFlame/releases/<完整commit>/` | 冻结程序、`uv.lock`、本机重建的 `.venv`；版本切换用完整路径，不自动移动 `current` 链接 |
| `~/Library/Application Support/KeepFlame/config.toml` | 服务器自有配置，0600；不嵌入密码/Cookie |
| `.../state/keepflame.sqlite3` | SQLite 与 WAL/SHM、执行锁；一个 runner 只用这一份数据库 |
| `.../profiles/joinquant-primary/` | 本机单独登记并本人登录的专用 Chrome，0700；不用普通浏览器或远程调试端口 |
| `~/Library/Logs/KeepFlame/` | 脱敏 stdout/stderr；目录0700，日志0600；与发布包和 profile 分开 |

候选 LaunchAgent 绑定具体提交的 Python，调用 `python -I -B -m keepflame run --config ...`，**没有 `--execute`**。它同时设 `Disabled=true`、`RunAtLoad=false`、`KeepAlive=false`，仅生成供审查，不能误认为已经装载或启用。配置所有 Flame 停用与 CLI dry-run 是两道独立门槛。真正执行需要服务器阶段另行批准后，操作者同时审查任务开关和执行参数。

## 3. 安装前只读核查

服务器在线、获准操作后，在服务器自身运行以下检查；开发机上的结果不能代替它。使用拟部署版本的 venv Python 时，才会检查到该环境的依赖元数据。

```sh
/Users/keepflame/Applications/KeepFlame/releases/<完整commit>/.venv/bin/python -I -B \
  /Users/keepflame/Applications/KeepFlame/releases/<完整commit>/deploy/prepare.py preflight \
  --server-home /Users/keepflame --commit "$RELEASE_COMMIT"
```

预检只报告 Python、已安装分发包版本、Chrome 可执行文件存在性，以及路径的类型/权限/所有者；不调用 Chrome、keeper、`health`、`status`，不读配置/SQLite/profile 的内容，也不遍历 profile。它始终保留 `ready_for_live_execution=false`，因为这些元数据不能证明登录、平台许可、图形操作或领取安全。尤其不能拿生产 `health` 代替这一步：现有该入口可能初始化数据库。

安装前逐项确认：

1. 非管理员运行用户已登录 Mac 图形会话；Python 3.13+、已安装 Chrome 与锁定的 Playwright/Pillow/NumPy 支持服务器 OS/CPU。服务器单独创建 venv，按 `uv.lock` 安装 browser 依赖，使用 frozen 模式且不更新锁。不要拷贝开发 venv，不自动下载浏览器；本工具不执行安装。
2. 发布包摘要与受审版本一致。目录所有者正确、运行目录0700、配置/日志0600、`umask 077`。先用全停用配置验证 dry-run；确认它未建 Chrome、未访问聚宽。
3. 在服务器正常出口上人工确认官方站点可达；地域限制时停止，不代理绕过。平台许可与账号风险需独立确认，技术验收不是平台许可。
4. 在服务器专用窗口独立登记和登录，使用下面的正式 `login` 控制流，不复制开发 Cookie/password/profile。保持同一 runner 的长期浏览器上下文；重启、登出或服务端过期仍可能需要本人重新登录。不能把会话 Cookie 导出成持久凭据规避这个边界。
5. 单独批准实际签到/阅读观察/领取的范围，核对固定任务、次数预算和后端完成证据。阅读尚未真实确认资格与领取时保持停用。结果不明时先读取状态或待人工，不按失败自动重做领取。
6. 先验证一次执行，再测试上海时区跨日、唤醒、重启重复保护、断网/403/429、需登录暂停。最后才批准常驻；开发机此后不运行相同生产账号任务。

### 服务器本机登录控制流

以下仅记录**服务器阶段另行获准后**的正式控制流，本次不执行，也不借此启用业务任务。先在已登录 GUI 的运行用户下启动同一常驻进程 `run --execute`；两个 Flame 仍可保持 `enabled=false`，以便先完成本机登录。随后请求指定 Flame 的15分钟登录窗口，由**这个 daemon 所有的同一浏览器上下文**打开官方登录页，无需临时 helper 或另开一个浏览器进程。

```sh
/Users/keepflame/Applications/KeepFlame/releases/<完整commit>/.venv/bin/keepflame \
  run --config "/Users/keepflame/Library/Application Support/KeepFlame/config.toml" --execute

# 在另一个本机终端请求窗口；记录返回的 REQUEST_ID。
/Users/keepflame/Applications/KeepFlame/releases/<完整commit>/.venv/bin/keepflame \
  login --config "/Users/keepflame/Library/Application Support/KeepFlame/config.toml" \
  --flame joinquant-primary --execute

# 本人在专用窗口完成登录后，确认对应的同一次请求。
/Users/keepflame/Applications/KeepFlame/releases/<完整commit>/.venv/bin/keepflame \
  login --config "/Users/keepflame/Library/Application Support/KeepFlame/config.toml" \
  --confirm REQUEST_ID --execute
```

确认后关闭的是登录页，保留 daemon 的浏览器上下文，再允许后台只读检查登录状态；确认命令本身不证明远端已登录，也不会启用停用中的阅读或领取任务。只有后端状态验证通过才恢复相应会话。登录请求过期、取消或 daemon 重启后不自动重开窗口；本人需要登录时重新发起明确请求。保持同一数据库与账号绑定，不把旧 REQUEST_ID 交给重启后的进程，也不把 `run --execute` 做成第二个并行 runner。

登录后，在同一配置文件中启用另行获准的 Flame，再运行 `keepflame reload --config <同一配置路径> --execute`。该命令只排本地请求；检查 `status --json` 的 `runtime_commands` 为 completed，才表示 daemon 已应用。重载保留原浏览器与动作意图，不通过重启来启用任务，不支持改变状态库路径。无效重载会停止派发，修正后再次明确请求；单纯编辑文件不会自动启用。

## 4. 启停、恢复与回退边界

初期使用运行用户的 LaunchAgent，依赖 Aqua 图形登录；不使用需要 root 的 LaunchDaemon，不开放公网或浏览器调试端口。用户登出会终止进程，主机系统睡眠时不能执行；显示器休眠不等于系统睡眠。服务器供电/睡眠策略、锁屏后 Chrome 行为必须现场核验，工具不会修改 `pmset` 或自动唤醒设置。

业务日界线、窗口与退避由 KeepFlame 调度；不要再用 `StartInterval`、cron 或系统日历反复调用 `run-once`。一个账户/任务只有一个生产 runner、一个数据库；文件锁无法替两个数据库或两台机器互锁。任务需登录时等待本人，不在后台重建浏览器反复试登；进程退出后先诊断再重启。

候选 `KeepAlive=false` 故**没有自动崩溃重启**。`ThrottleInterval=300` 只是为后续审查保留节流值，不是重启次数上限；不要把它当成防止无限重试。`ExitTimeOut=240` 给最长120秒阅读、60秒操作及关闭自有浏览器留出余量。常驻进程收到 SIGTERM 后停止接新任务，等待当前一轮受限流程收尾，再关闭自己拥有的浏览器；不能在领取途中仅因45秒退出期限而提前杀死进程。若仍被强制终止，恢复须遵守已经落盘的执行意图/不明结果状态。

版本切换或回退均是后续人工批准动作，没有一键切换脚本：先停用并等待唯一旧 runner 退出，保留上一提交及候选配置；停服后用 SQLite 一致性备份（或 SQLite 在线备份 API）保存状态，不能只拷贝活跃主 `.sqlite3` 漏掉 WAL。备份不含 profile、凭据或认证页面。只改变 plist 的完整 release 路径；不覆盖旧代码、不搬状态、不删除目录。

旧版本未必理解新状态/执行意图，**schema 不兼容时不能直接回退执行**。保留新数据库只读副本，先用停用配置验证旧版本兼容性；无法证明时保持停用，不通过删状态/恢复旧成功记录来“修复”。需要恢复状态备份也必须单独决定，并先对照远端，防止回退后重复领取。

## 5. 日志与证据保留

本轮不添加删除任务或后台清理器。LaunchAgent 直接重定向的文件不会自动轮转；上线前应确定运维责任，不能无限放任增长。建议普通脱敏运行日志按日封存、保留14天且总量不超过50 MiB；涉及结果不明、领取异常的最小脱敏证据单独标记，保留至问题确认，再人工决定清理。

第一阶段采用停服后轮转：先确认 runner 已退出，再只重命名 `~/Library/Logs/KeepFlame/runner.out.jsonl` 和 `runner.err.jsonl`，新日志0600；按以上期限列出候选，经审查后只处理这两个自有日志系列。不能向进程发送默认 SIGHUP 作为“轮转”、递归清理整个 Library、清 Chrome cache/profile、删除状态库或现存验收证据。本工具从不执行这些轮转/删除步骤；日志配额与轮转仍属于服务器启用前验收项。

## 离线验证

`tests/test_deploy.py` 在临时合成 Git 仓库和运行目录检查：默认零写入、冻结提交/干净树、忽略账号文件、敏感路径/链接拒绝、确定性摘要、篡改/穿越/重复条目拒绝、候选默认停用、预检只看元数据且不启动进程。测试不会访问真实 profile、外网、真实 SQLite 或其他项目端口；通过不代表服务器部署验收完成。
