# Notifier

通知层把统一执行结果整理为摘要或可操作告警，并投递到后续选定的通知渠道。

这里负责格式与投递，不重新判断 Keeper 的成功、失败或 `WAITING_HUMAN` 状态。常规成功应尽量汇总，避免产生通知噪声。

生命周期投递始终写 stdout JSONL：首次人工操作、当日一次提醒、失败变化、已证明积分和恢复才投递；每次复检的完整结果由 storage 保留。`Notifier.notify` 接收稳定 `notification_id`。可选 SMTP 邮箱渠道在 JSONL 之外再发一封短讯，只在 `[notify.email] enabled = true` 时启用；省略或关闭时不打开套接字，也不读取密码文件。发布样例保持关闭，不包含真实邮箱或邮箱口令。口令只在发送时从绝对路径密钥文件读取，末尾单个换行会被去掉；口令不进入配置示例、日志、异常文本或邮件正文。进程事件仍只写 JSONL。

可选 Slack 渠道（`slack.py`）同样在 JSONL 之外发一条短讯，只在 `[notify.slack] enabled = true` 时启用；省略或关闭时不联网，也不读取 Webhook 文件。它用标准库 urllib 向 Incoming Webhook POST `{"text": ...}`，不跟随重定向，超时取 `timeout_seconds`。消息只含 Flame、账号标识、事件类型、状态、一句话说明、下一步时间、人工动作的类型/摘要/说明和通知 ID；积分入账事件另带数量与项目。不含 metadata、URL、Cookie、token 或页面内容，并转义 Slack 的 `&`、`<`、`>`，单字段限长。Webhook 地址保存在仓库外的绝对路径文件，只在发送时读取，只接受 `https://hooks.slack.com/` 开头的地址；它不进配置对象、日志、异常、消息正文或 outbox，错误只记类型。

邮箱与 Slack 可同时开启，也可只开其一。`LifecycleNotifier` 逐个渠道投递并各自捕获失败：一个渠道失败不会阻止另一个发送；失败时整个事件仍按 outbox 重试，已成功的渠道会按 `notification_id` 在进程内跳过，不重复发送。两个渠道同时失败时记 `ChannelDeliveryError`，只列渠道名。进程重启后的至少一次重复仍可能出现，渠道可凭通知 ID 去重。

待发送事件与状态同事务保存，交付失败最多尝试 5 次；确认成功后才标记已提醒。至少一次交付在崩溃边界可能重复，渠道可按 `notification_id` 去重。开发测试注入传输，不连接真实邮箱。
