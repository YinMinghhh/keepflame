# Configuration

这里存放可提交的配置示例、默认值和校验规则，用于声明 Flame、账号引用、启用状态、执行计划、时区和插件参数。

真实 Token、Cookie、密码、会话文件以及包含个人账号信息的运行配置不得提交到 Git。

`keepflame.example.toml` 是可提交的安全示例。复制后的真实配置应命名为 `*.local.toml` 或放在仓库外；聚宽配置只引用 Cookie 密钥文件路径，不在 TOML 中内嵌 Cookie。

每个 Flame 必须显式声明唯一 `id`、`keeper`、`account_id`、启用状态和带时区的每日检查窗口。配置加载器拒绝未知字段、重复 Flame ID 和顺序不合法的时间窗口。
