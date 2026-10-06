# JoinQuant Flame

这个 Keeper 只读取聚宽账号当天的签到任务状态。它使用仓库外的 Cookie 密钥文件调用聚宽状态接口，并把结果映射为统一的 `SUCCESS`、`FAILED` 或 `WAITING_HUMAN`。

每日签到需要聚宽滑块验证码。首版不会请求验证码、计算滑块位置或调用积分领取接口；未签到时只给出官方积分页面并等待用户完成操作，后续复检负责确认结果。

Cookie 内容不得进入 Git、日志、SQLite 或测试 fixture。每个账号必须使用独立 `account_id` 和独立密钥文件。

