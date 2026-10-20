# Tushare 每日积分

`keeper = "tushare"` 用人工导入的 tushare.pro 会话 Cookie 完成个人中心的三项每日积分任务。三项拆成三个 Flame，共用同一个账号和 Cookie 文件，按 `settings.task` 区分（参照聚宽签到/阅读的拆法）：

| `task` | 任务 | 积分 | 接口 |
| --- | --- | --- | --- |
| `sign` | 每日签到 `DAILY_SIGN` | +1 | `POST /wctapi/user_center/sign`（无 body） |
| `view` | 浏览上新数据 `VIEW_NEW_DATA` | +2 | 先 `GET /document/1?doc_id=9`，再 `POST /wctapi/user_center/view` |
| `guess` | 猜明日涨跌（上证指数） | 参与 +1，猜对再 +4 | `GET /wctapi/user_center/guess/current`，`POST /wctapi/user_center/guess/vote` |

登录、验证码和 Cookie 更新始终由人完成。本插件不破解验证码、不自动重登、不用数据 API token 调网页接口、不做多账号，也不自己判断涨跌（只跟随多数）。

## 流程

1. 读取 `cookie_file`（绝对路径、仓库外、mode 600、属主一致、非符号链接/硬链接，由 `FileSecretResolver` 强制）。文件是 `{"version":1,"origin":"https://tushare.pro","cookie":"..."}`。
2. 所有请求只发往 `https://tushare.pro`，带 `Origin: https://tushare.pro`、`Referer: https://tushare.pro/weborder/` 和浏览器 UA。Cookie **从不发送到其他域名**。
3. `sign` / `view`：先 `GET /wctapi/user_center/tasks`，`task_code` 对应项 `is_completed=true` 就直接记 SUCCESS（`already_completed`），不再调用动作接口。否则调用动作接口，再读一次任务列表核对 `is_completed`（`verify_after_action`，默认开）。
4. `guess`：读当期 `period` 和 `user_guess`。
   - `user_guess.vote` 已有值 → SUCCESS（`already_voted`）。
   - `period.status != 1`（2 待开奖、3 已开奖）→ SUCCESS（`not_open`），不投票。
   - `status == 1` 且未投 → 投票数多的一方：`up_votes > down_votes` 投 `{"vote":1}`（看涨），反之投 `{"vote":2}`（看跌）。
   - 票数相等：照样投票，从不跳过。默认 `guess_tie = "random"`，随机投看涨或看跌；可改成 `"up"` 或 `"down"` 固定一边。
   - 每次实际投票都写一行日志 `tushare_guess_vote {...}`（方向、看涨/看跌票数、是否平票、平票策略，不含 Cookie），同样的字段（`vote`、`vote_direction`、`up_votes`、`down_votes`、`tie`、`tie_policy`）也记在运行记录的元数据里。
5. 结果分类：
   - 本次完成 → SUCCESS，`reward` 记本次领取的积分（签到 1、浏览 2、竞猜参与 1）。
   - 已完成 / 重复（任务列表 `is_completed`，或返回文案含“已达上限/已完成/已签到”等）→ SUCCESS，不重复调用。
   - 接口返回 `code == 40201`（未找到登录信息，HTTP 200）、401/403、跳转到登录页 → `WAITING_HUMAN`（`session_expired`），提醒里写明登录链接 `https://tushare.pro/weborder/#/login` 和导入命令。Slack 只给不带 `#`/`?` 的链接加 `Action-URL`，所以可点链接是 `https://tushare.pro/weborder/`（未登录时网页会自己跳到登录页）。
   - Cookie 文件缺失或权限不对 → `WAITING_HUMAN`（`cookie_unreadable`）；格式不对 → `cookie_invalid`。
   - 网络层错误（没有拿到任何 HTTP 响应：连接失败、TLS 断开如 `SSLEOFError`、超时、连接被重置、响应读一半断开）先在**本次运行内**重试，见下文“网络层重试”。
   - 其他非 200、非 JSON、`code != 0`、`success=false`、动作后任务仍未完成、网络层重试用尽 → FAILED，按 `retry_minutes` 有限重试，用尽后当天发一次 `retries_exhausted`。因为每次都先查已完成状态，重试不会重复领取。
   - 配置无效 → FAILED 并暂停自动化。
6. `WAITING_HUMAN` 立即发 `human_action_required`，之后每个新业务日复查并再次提醒；同一天 Cookie 未变化时不联网。三个 Flame 各自判断，Cookie 失效时会各发一次提醒；重新导入一次 Cookie 三个都恢复。

## 网络层重试

Mac mini 上约三分之一的 tushare.pro 请求会在约 5 秒后以 `SSLEOFError`（UNEXPECTED_EOF_WHILE_READING）失败，走代理和直连都一样。所以同一次运行内对网络层错误做有限重试：

- 只重试**没有拿到 HTTP 响应**的错误。HTTP 4xx/5xx、非 JSON、`code != 0`、`code == 40201` 都不在本次运行内重试。
- GET（`tasks`、`guess/current`、`doc_id=9` 页面）直接重试，最多 `transport_retries` 次（默认 2），每次间隔 `transport_backoff_seconds × 第几次`（默认 2 秒、4 秒）。
- POST（`sign`、`view`、`guess/vote`）**从不盲目重发**：响应丢了不代表请求没到服务器。等待退避后先重新读远端状态：
  - `sign` / `view`：重读任务列表，`is_completed=true` 就记 SUCCESS（`completed_after_transport_error`，本次领取）并停止；仍未完成才重发一次 POST。
  - `guess`：重读 `guess/current`，已有投票就停止（是我们投的方向记 `voted_after_transport_error`，否则记 `already_voted`）；竞猜已关闭记 `not_open`；仍开放且未投才用**同一方向**重发（平票时不会重新抽签）。
- 每次网络层错误写一行日志 `tushare_transport_error {...}`（步骤、方法、异常类型、第几次、上限），元数据记 `error_type`（例如 `SSLEOFError`）、`transport_step` 和 `<步骤>_transport_attempts`，不含 URL 参数、请求头或 Cookie。
- 共享的 `UrllibHttpClient` 现在把所有“无响应”错误（含读响应时的 TLS/连接错误）都归为 `HttpTransportError`，并带 `error_type`。重试和 POST 安全判断留在 Keeper 里；ikuuu 目前只在失败元数据里记录 `error_type`，不在本次运行内重试（它的签到没有可先查的已完成接口）。

## 配置

见 `config/keepflame.tushare.example.toml`。三个 Flame 的 `cookie_file` 填同一个路径；`view` 和 `guess` 用 `after_flame = "tushare-sign"` 排在签到之后（`after_flame` 只支持一层，所以两者都锚定签到）。竞猜在 T 日 13:00 截止，示例窗口 09:00–10:30，竞猜重试 20/40/60 分钟，最晚约 12:40 前结束。

| 键 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- |
| `task` | 是 | — | `sign` / `view` / `guess` |
| `cookie_file` | 是 | — | 绝对路径，放在仓库外；三个 Flame 共用 |
| `task_code` | 否 | `DAILY_SIGN` / `VIEW_NEW_DATA` | 任务列表里的 `task_code`，仅 `sign` / `view` |
| `verify_after_action` | 否 | `true` | 动作后再读任务列表，要求 `is_completed=true` |
| `guess_tie` | 否 | `random` | 仅 `guess`：平票时 `random`（随机）/ `up` / `down`，总是投票 |
| `timeout_seconds` | 否 | 15 | 3–60 |
| `request_pause_seconds` | 否 | 1.0 | 0–10，请求之间的间隔 |
| `transport_retries` | 否 | 2 | 0–5，本次运行内网络层错误的重试次数（POST 每次重发前先重读状态） |
| `transport_backoff_seconds` | 否 | 2.0 | 0–30，第 n 次重试前等待 n 倍这个秒数 |

## 导出并导入 Cookie

1. 在浏览器打开 <https://tushare.pro/weborder/#/login>，手动登录（图形验证码、短信或微信扫码都可以），确认能打开个人中心 <https://tushare.pro/weborder/#/user/privilege>。
2. 任选一种方式复制 Cookie：
   - DevTools → Network → 刷新个人中心 → 选中任一 `/wctapi/...` 请求（例如 `user_center/tasks`）→ Request Headers → 复制 `Cookie` 的值；或
   - Cookie-Editor 等扩展 → Export → JSON。导入时只保留 `tushare.pro` 域名的条目，其他域名的 Cookie 会被丢弃。
3. 在 KeepFlame 主机执行（macOS）：

   ```bash
   pbpaste | keepflame tushare import-cookie --config /path/to/keepflame.toml
   # 三个 Flame 的 cookie_file 不同时加 --flame，例如 --flame tushare-sign
   # 或：keepflame tushare import-cookie --config /path/to/keepflame.toml --from-clipboard
   # 终端交互时不加管道，会以不回显方式提示粘贴
   ```

   命令把文件原子写为 mode 600，拒绝仓库内路径和符号链接，只打印 Cookie 名称与指纹，不打印值。下一次计划运行即会继续三项任务，也可以 `keepflame run-once --flame tushare-sign --execute` 立即试一次。

4. 在别处退出登录或修改密码可能让这份 Cookie 失效；失效时会收到“Tushare 登录会话已失效”提醒，重复以上步骤即可。

## 待登录核对（T0）

以下几点只能在登录后确认，代码里用宽松解析和配置项兜底，确认后再收紧：

- 会话 Cookie 名称、有效期、是否 HttpOnly，有无 CSRF Cookie/请求头（目前未发送任何 CSRF 头）。
- `tasks` 登录后的返回格式（目前按 `data.datas[].task_code / is_completed` 解析），`VIEW_NEW_DATA` 的 `limit_times=0` 含义。
- 签到成功与重复签到的返回原文。**已确认（真实运行）**：签到和浏览上新成功都返回「任务完成，积分已发放」。重复时的原文仍待确认（目前按“已达上限/已完成/已签到”等匹配）。
- 浏览上新是否只调 `view` 就算完成，还是要先访问 `doc_id=9` 页面（目前两者都做）。
- 投票请求与返回、积分记录文案。
- 周末能否签到和浏览；`guess/current` 周末显示哪一期。
- Cookie 实际能用几天；在别的设备登录是否会把 Mac mini 上的会话踢掉。

## 风险

积分规则写过“通过技术手段获得的积分会被清理”，用户协议也有违规可封禁的兜底条款，自动化有被清理积分或限制账号的可能，风险由账号所有人自担。个人中心 2026-04 才上线，接口可能变动；接口改了就失败告警，不猜测修补。
