# Configuration

这里存放可提交的配置示例、默认值和校验规则，用于声明 Flame、账号引用、启用状态、执行计划、时区和插件参数。

真实 Token、Cookie、密码、会话文件以及包含个人账号信息的运行配置不得提交到 Git。

`keepflame.example.toml` 是可提交的安全示例。复制后的真实配置应命名为 `*.local.toml` 或放在仓库外；聚宽配置只引用 Cookie 密钥文件路径，不在 TOML 中内嵌 Cookie。

每个 Flame 必须显式声明唯一 `id`、`keeper`、`account_id`、启用状态和带时区的每日检查窗口。配置加载器拒绝未知字段、重复 Flame ID 和顺序不合法的时间窗口。

`keepflame.browser.example.toml` 声明但默认停用聚宽自动模式，引用已登记的专用浏览器目录。此目录存储登录会话，应留在仓库外；配置不内嵌 Cookie 或密码。真实运行前先完成 JoinQuant 目录说明中的一次会话交接。

可选 `[notify.email]` 声明 SMTP 生命周期通知。省略整段即关闭。样例里的该段保持 `enabled = false`，主机和地址只用 `example.invalid`，口令只写绝对路径，不写口令本身，也不连接真实邮箱。打开时必须给出主机、端口、`starttls`/`ssl`/`none`、发件人、至少一个收件人，以及绝对路径的 `password_file`；`timeout_seconds` 省略时为 15，允许范围是 1 到 120。未知字段在加载时拒绝。关闭或省略都不会读取口令文件。

可选 `[notify.slack]` 声明 Slack Incoming Webhook 通知，省略整段即关闭。样例里的该段保持 `enabled = false`，`webhook_file` 只写绝对路径，样例不含任何真实 Webhook 地址，也不连接真实 Slack。该路径必须是绝对路径且位于仓库之外（符号链接指入仓库也拒绝），文件内只放一行 Webhook 地址，地址只在发送时读取，只接受以 `https://hooks.slack.com/` 开头的地址，其他一律拒绝；配置错误信息不回显路径内容或地址。`timeout_seconds` 省略时为 15，允许范围是 1 到 120。未知字段在加载时拒绝。关闭或省略都不会读取该文件，也不联网。邮箱与 Slack 可同时开启或只开其一。

现在所有样例默认停用，且 CLI run/run-once 必须显式 --execute 才能运行。`task="checkin"` 与 `task="reading"` 独立配置，不允许同 keeper/account/task 重复。阅读只接受指定公开文章 URL，禁止搜索、自动挑选或付费文章；字段和边界由聚宽插件校验。服务器候选见 deploy/。

`keepflame.ikuuu.example.toml` 声明但默认停用 ikuuu 每日签到，只引用仓库外的 Cookie 文件路径和公开导航页种子；字段见 `src/keepflame/flames/ikuuu/README.md`。
