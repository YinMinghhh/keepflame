# Capabilities

能力层封装可被多个 Flame 复用的技术操作，例如 HTTP 客户端、浏览器自动化、Telegram 客户端、密钥读取和限流重试。

能力层不包含具体平台业务规则，也不决定任务是否启用或何时运行。

首版包含：不自动跟随重定向的标准库 HTTP 客户端、每次读取且不缓存内容的文件 Secret 解析器，以及小型 Capability registry。HTTP capability 不解释聚宽响应，也不决定哪些请求可以重试。Secret 文件必须是当前用户拥有的绝对路径普通文件，权限不得对组或其他用户开放（`mode & 0o077 == 0`，通常为 0600）；符号链接与硬链接拒绝。错误信息不回显文件内容。

`browser.py` 管理 KeepFlame 自己的 Chrome 持久会话。只有仓库外、当前用户专属且显式登记账号的目录可用，不扫描端口或连接已有浏览器。依赖在使用时才加载；默认安装及 `health` 不启动 Chrome。能力层负责会话关闭、请求域名限制和浏览器安全选项，不包含聚宽选择器或签到判断。

profile 登记还检查父目录可控、内部数据链接和所有权；扫描仅用文件元数据，不读浏览器数据内容。Chrome 根目录的 SingletonLock/SingletonSocket/SingletonCookie，以及只含版本号的 RunningChromeVersion 链接不视为数据逃逸。聚宽请求预算留在聚宽插件的 guard.py，不扩展通用 browser capability 为站点防火墙。

持久目录本身不保留没有有效期的会话 Cookie。`IsolatedBrowser.session(...)` 只把本次响应里的 HttpOnly、且没有 Expires 和 Max-Age 的 Cookie 记在该 profile 内，并补上有限有效期，供同一 profile、同一 origin 的下一个浏览器进程在同源请求中送出。已有有效期的 Cookie 仍交给浏览器保存。这份记录不写出 profile、不进日志、不进入其他账号的 profile，也不调用 Cookie 导出接口。服务器拒绝登录时结果仍是等待人工，本地记录不是登录成功。接入程序可在该 session 内让用户自行登录，然后把返回的 `OwnedBrowserSession` 传给 `build_runtime(..., browser=session)`，在同一个仍存活的浏览器中执行一次 Keeper。该句柄只允许原 profile、account_id、origin 使用一次；失败也消耗这次使用，退出外层上下文后失效并关闭浏览器。它保留登录窗口原来的显示模式，不重开浏览器、不扫描现有窗口或导出 Cookie。普通 `open()` 仍提供单次上下文；CLI 的常驻行为见下文。

CLI 执行器现使用 BrowserPool：同一进程保留每个 profile 的唯一上下文，检查 profile/account/origin 绑定，每轮关闭全部任务页，仅进程关闭时退出 Chrome。登录窗口与任务页串行；账号忙碌时拒绝借用。默认 GUI 模式供未来官方人工登录，测试可显式 headless。`open(headless=False)` 即使池按 headless 构造也会可见启动；省略该参数时仍沿用构造时的 headless。账号上限8，不自动重开失效上下文；单次 OwnedBrowserSession 接口仍供受控集成使用。

`keepflame run --execute` 对每个已启用浏览器 profile 持有一把独占锁，并先打开一个可见窗口。签到和阅读共用一个 profile 时只启动一个窗口，人工登录和两件每日任务都在这个窗口里进行。第二个启动在校验 profile 内容和启动 Chrome 之前退出。锁在本进程关闭浏览器之后才释放。

服务器首次登记专用目录时（仅在另行获准后），目录必须是当前运行用户拥有的绝对路径、0700，父目录不可组/其他用户写，且位于任何Git仓库之外。新空目录内创建0600的 `.keepflame-profile.json`，内容严格为 `{"version":1,"account_id":"primary","purpose":"keepflame"}`，account_id须与配置一致。不要重用日常Chrome目录或修改已有profile归属。该标记只登记目录身份，不含凭据也不证明已登录；随后再用正式login控制流。

池同时约束 account/origin 只能使用一个 profile。空闲、非当前拥有页、popup和worker的网络默认拒绝；结束任务先关闭所有页再撤插件门禁，关闭失败则保留门禁并停止整个自有池。底层原生请求禁用自动重定向，避免浏览器重定向跳过后续route检查；登录遇到需跳转的新流程时也会停止，不能临时放宽。

## 可选的原生会话恢复

`keepflame run --restore-browser-session --execute --config <path>` 复用现有常驻 owner；
未加 `--execute` 时只做 dry-run。恢复开关仅供 daemon，不提供给一次性 `run-once`。
`run` 仍按配置执行启用的 Flame，**它不是纯认证检查命令**；只验证保存时必须使用已审核的
禁用任务配置，不能把此命令直接用于真实奖励验收。代码接入、登录验证和服务部署分别授权。
API 等价入口为 `build_runtime(..., restore_browser_session=True)`，不可同时传入借用的 browser。

恢复保留同一 profile 和 Playwright 原有加密参数，不读取/导出 Cookie 或 storage_state，
不改系统钥匙串项或系统网络设置。没有有效期的 HttpOnly 会话 Cookie 只在该 profile 内补上有限有效期；已经带有效期的 Cookie 不改写。启动前先占用一个不监听的本机 TCP 端口，
Chrome 唯一代理指向此端口并禁用 loopback 绕过、直接 DNS、QUIC 和非代理 WebRTC UDP；
屏障持续到浏览器关闭。仅安装 launch 后的 route 不构成启动屏障。

Chrome 恢复后先全拒绝、关闭恢复页，再启用现有 origin/插件门禁。只有 owner 明确授予的
新页面可进入 Node 侧 `route.fetch` 转发；迟到的恢复页、popup、worker 和空闲页面均拒绝。
浏览器原始网络始终被代理屏障阻断。原生 API transport 不走 Chrome CLI 代理，仍由调用方
的只读/业务门禁约束；此能力不把任意 API 请求自动变成安全请求。

普通任务失败或暂停后关闭业务页并保留空闲 context；无 idle TTL，不刷新或轮询网站保活。
有界任务通过现有 daemon 客户端完成，客户端退出不关闭 owner。SIGINT/SIGTERM、程序退出、
owner 故障和门禁清理失败仍关闭自有资源；停止日志记录 `user_stop`、`program_exit`、
`owner_error` 或 `security_cleanup_failed`。进程必须由用户明确运行的长期 owner 持有；
此改动不安装服务，也不承诺机器睡眠、崩溃或远端撤销后仍有有效认证。

合成本机 TLS/独立进程恢复试验只验证浏览器机制。真实账号必须另行验证关闭前和重启后
后端认证状态；不能由合成成功推断真实 10001 的原因或宣称每日免登录已验收。

恢复初始化另有共享 20 秒 driver 请求预算，关闭 context 有 5 秒请求预算。`route()` 等
没有公开 timeout 参数，`set_default_timeout` 对这些调用无效；适配器仅在本次自有
Playwright connection 上补齐 protocol timeout metadata，退出相应阶段立即恢复。
此私有接口只对已复核的 Playwright **1.63.0** 启用，版本/签名不匹配即拒绝恢复，
不静默回退为无截止调用。driver EOF 使用该版本内建的最多 30 秒关闭兜底；代理端口
在 driver 退出前保持占用。没有外部扫描或终止其他浏览器，没有改变加密提供者。

初始化截止/失败后不交付 context，pool 以 `restore_startup_failed` 关闭并拒绝再次启动；
拥有它的 runtime 停止后续控制/任务派发，保留既有 pending 和暂停。普通业务错误仍沿用
保留 context 的行为。本机制处理可响应的 Playwright driver 中等待浏览器操作的超时；
不把它宣称为操作系统进程冻结或 Python/driver CPU 死锁的通用 watchdog。
