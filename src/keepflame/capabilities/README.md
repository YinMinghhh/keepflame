# Capabilities

能力层封装可被多个 Flame 复用的技术操作，例如 HTTP 客户端、浏览器自动化、Telegram 客户端、密钥读取和限流重试。

能力层不包含具体平台业务规则，也不决定任务是否启用或何时运行。

首版包含：不自动跟随重定向的标准库 HTTP 客户端、每次读取且不缓存内容的文件 Secret 解析器，以及小型 Capability registry。HTTP capability 不解释聚宽响应，也不决定哪些请求可以重试。
