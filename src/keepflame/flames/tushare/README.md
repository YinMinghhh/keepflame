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
   - 票数相等：默认 `guess_tie = "defer"`，本次记 FAILED（`guess_tie_deferred`），按 `retry_minutes` 稍后重看，等别人投出差距再跟。可改成 `"up"` 或 `"down"` 固定一边。
5. 结果分类：
   - 本次完成 → SUCCESS，`reward` 记本次领取的积分（签到 1、浏览 2、竞猜参与 1）。
   - 已完成 / 重复（任务列表 `is_completed`，或返回文案含“已达上限/已完成/已签到”等）→ SUCCESS，不重复调用。
   - 接口返回 `code == 40201`（未找到登录信息，HTTP 200）、401/403、跳转到登录页 → `WAITING_HUMAN`（`session_expired`），提醒里写明登录链接 `https://tushare.pro/weborder/#/login` 和导入命令。Slack 只给不带 `#`/`?` 的链接加 `Action-URL`，所以可点链接是 `https://tushare.pro/weborder/`（未登录时网页会自己跳到登录页）。
   - Cookie 文件缺失或权限不对 → `WAITING_HUMAN`（`cookie_unreadable`）；格式不对 → `cookie_invalid`。
   - 其他非 200、非 JSON、`code != 0`、`success=false`、动作后任务仍未完成、网络错误 → FAILED，按 `retry_minutes` 有限重试，用尽后当天发一次 `retries_exhausted`。因为每次都先查已完成状态，重试不会重复领取。
   - 配置无效 → FAILED 并暂停自动化。
6. `WAITING_HUMAN` 立即发 `human_action_required`，之后每个新业务日复查并再次提醒；同一天 Cookie 未变化时不联网。三个 Flame 各自判断，Cookie 失效时会各发一次提醒；重新导入一次 Cookie 三个都恢复。

## 配置

见 `config/keepflame.tushare.example.toml`。三个 Flame 的 `cookie_file` 填同一个路径；`view` 和 `guess` 用 `after_flame = "tushare-sign"` 排在签到之后（`after_flame` 只支持一层，所以两者都锚定签到）。竞猜在 T 日 13:00 截止，示例窗口 09:00–10:30，竞猜重试 20/40/60 分钟，最晚约 12:40 前结束。

| 键 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- |
| `task` | 是 | — | `sign` / `view` / `guess` |
| `cookie_file` | 是 | — | 绝对路径，放在仓库外；三个 Flame 共用 |
| `task_code` | 否 | `DAILY_SIGN` / `VIEW_NEW_DATA` | 任务列表里的 `task_code`，仅 `sign` / `view` |
| `verify_after_action` | 否 | `true` | 动作后再读任务列表，要求 `is_completed=true` |
| `guess_tie` | 否 | `defer` | 仅 `guess`：`defer` / `up` / `down` |
| `timeout_seconds` | 否 | 15 | 3–60 |
| `request_pause_seconds` | 否 | 1.0 | 0–10，请求之间的间隔 |

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
- 签到成功与重复签到的返回原文（重复文案目前按“已达上限/已完成/已签到”等匹配）。
- 浏览上新是否只调 `view` 就算完成，还是要先访问 `doc_id=9` 页面（目前两者都做）。
- 投票请求与返回、积分记录文案。
- 周末能否签到和浏览；`guess/current` 周末显示哪一期。
- Cookie 实际能用几天；在别的设备登录是否会把 Mac mini 上的会话踢掉。

## 风险

积分规则写过“通过技术手段获得的积分会被清理”，用户协议也有违规可封禁的兜底条款，自动化有被清理积分或限制账号的可能，风险由账号所有人自担。个人中心 2026-04 才上线，接口可能变动；接口改了就失败告警，不猜测修补。
