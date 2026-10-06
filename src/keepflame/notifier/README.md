# Notifier

通知层把统一执行结果整理为摘要或可操作告警，并投递到后续选定的通知渠道。

这里负责格式与投递，不重新判断 Keeper 的成功、失败或 `WAITING_HUMAN` 状态。常规成功应尽量汇总，避免产生通知噪声。

默认实现仍是 stdout JSONL：首次人工操作、当日一次提醒、失败变化和恢复才投递；每次复检的完整结果由 storage 保留。`Notifier.notify` 接收稳定 `notification_id`，可通过 runtime 的 `delivery_notifier` 注入渠道；测试用 fake 捕获，尚未绑定实际收件人。待发送事件与状态同事务保存，交付失败最多尝试 5 次；确认成功后才标记已提醒。至少一次交付在崩溃边界可能重复，渠道可按 ID 去重。
