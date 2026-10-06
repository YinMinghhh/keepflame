# KeepFlame package

这是 KeepFlame 的生产包，围绕“检查并维持 Flame”组织代码。

- `core/` 定义生命周期和领域契约；
- `flames/` 实现具体平台插件；
- `capabilities/` 提供可复用的技术能力；
- `storage/` 保存状态与执行历史；
- `notifier/` 负责通知呈现和投递。

命令行入口或守护进程入口建立后也应位于本包内，而不是散落在仓库根目录。

当前 `cli.py` 提供 `run`、`run-once`、`status` 和 `health`，`runtime.py` 是具体 Capability、Storage、Notifier 与 Core 的唯一组装入口。
