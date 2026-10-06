# Capabilities

能力层封装可被多个 Flame 复用的技术操作，例如 HTTP 客户端、浏览器自动化、Telegram 客户端、密钥读取和限流重试。

能力层不包含具体平台业务规则，也不决定任务是否启用或何时运行。

首版包含：不自动跟随重定向的标准库 HTTP 客户端、每次读取且不缓存内容的文件 Secret 解析器，以及小型 Capability registry。HTTP capability 不解释聚宽响应，也不决定哪些请求可以重试。

`browser.py` 管理 KeepFlame 自己的 Chrome 持久会话。只有仓库外、当前用户专属且显式登记账号的目录可用，不扫描端口或连接已有浏览器。依赖在使用时才加载；默认安装及 `health` 不启动 Chrome。能力层负责会话关闭、请求域名限制和浏览器安全选项，不包含聚宽选择器或签到判断。

profile 登记还检查父目录可控、内部数据链接和所有权；扫描仅用文件元数据，不读浏览器数据内容。Chrome 根目录的 SingletonLock/SingletonSocket/SingletonCookie 链接不视为数据逃逸。聚宽请求预算留在聚宽插件的 guard.py，不扩展通用 browser capability 为站点防火墙。

持久目录不保证没有有效期的会话 Cookie 在 Chrome 退出后保留。接入程序可在 `IsolatedBrowser.session(...)` 内让用户自行登录，然后把返回的 `OwnedBrowserSession` 传给 `build_runtime(..., browser=session)`，在同一个仍存活的浏览器中执行一次 Keeper。该句柄只允许原 profile、account_id、origin 使用一次；失败也消耗这次使用，退出外层上下文后失效并关闭浏览器。它保留登录窗口原来的显示模式，不重开浏览器、不扫描现有窗口或导出 Cookie。普通 `open()` 和 CLI 的默认行为不变。
