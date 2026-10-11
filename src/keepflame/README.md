# KeepFlame package

这是 KeepFlame 的生产包，围绕“检查并维持 Flame”组织代码。

- `core/` 定义生命周期和领域契约；
- `flames/` 实现具体平台插件；
- `capabilities/` 提供可复用的技术能力；
- `storage/` 保存状态与执行历史；
- `notifier/` 负责通知呈现和投递。生命周期事件写 JSONL；邮箱渠道默认关闭，不连接真实邮箱。

命令行入口或守护进程入口建立后也应位于本包内，而不是散落在仓库根目录。

当前 `cli.py` 提供 `run`、`run-once`、`status` 和 `health`，`runtime.py` 是具体 Capability、Storage、Notifier 与 Core 的唯一组装入口。

`run-once --via-daemon --flame ID --request-id UUID` 将单次任务送到现有 daemon 的 SQLite 队列，仍需 `--execute` 才入队。Runtime 持有执行锁和进程内浏览器池，串行处理有界请求；客户端不建立 Runtime、不借用调试端口，也不关闭 owner 的上下文。所有者退出收拢请求并关闭自有资源，借用 Runtime 不关闭外部 pool。没有跨进程浏览器恢复或凭据复制。状态和 stdout 生命周期事件仅记录本地请求/owner 标识、固定状态与原因。
