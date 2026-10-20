# ikuuu 每日签到

`keeper = "ikuuu"` 每天用人工导入的会话 Cookie 调一次 `POST /user/checkin`，记录返回的 `ret`/`msg` 和获得流量。登录、验证码（Geetest）和 Cookie 更新始终由人完成；本插件不做密码登录、不调用打码服务、不拉订阅、不测速。

## 流程

1. 读取 `cookie_file`（绝对路径、仓库外、mode 600、属主一致、非符号链接/硬链接，由 `FileSecretResolver` 强制）。文件是 `{"version":1,"base_url":"https://ikuuu.pw","cookie":"..."}`，Cookie 与导入时的域名绑定。
2. 域名发现：依次探测导入域名、`base_url` 配置、上次成功的域名；不可用时抓取 `nav_seeds` 导航页，静态解码其中 obfuscator.io 混淆脚本（纯 Python，不执行 JS）并补充明文中的 `ikuuu.*` 主机，再逐个 `GET /auth/login`。只有 200、最终主机不变、且页面（含 `originBody` base64 内容）带邮箱/密码登录表单才算可用；导航镜像对任何路径都返回导航页，因此不会被误判。请求间隔 `request_pause_seconds`，最多 `max_probes` 次探测。
3. 绑定域名可用时才发送签到，Cookie **从不发送到其他域名**。发现的新域名与绑定域名不同时转为 `WAITING_HUMAN`（`domain_changed`），需要在新域名重新登录并导入。
4. 结果分类：
   - `ret == 1` → SUCCESS，元数据含 `traffic_gained`、`traffic_gained_mb`、`checked_in_at`。
   - 消息为“已经签到/已签到” → SUCCESS（`already_checked_in`）。
   - 302 到 `/auth/*`、401/403、返回登录页或“请登录”消息 → `WAITING_HUMAN`（`session_expired`）。
   - 其他非 200、非 JSON、`ret != 1`、网络错误、找不到可用域名 → FAILED，按 `retry_minutes`（默认 30/60/120）有限重试，用尽后当天发一次 `retries_exhausted`。
   - 配置无效 → FAILED 并暂停自动化。
5. `WAITING_HUMAN` 立即发 `human_action_required`，之后每个新业务日复查并再次提醒；同一天 Cookie 未变化时不联网。Slack 消息附 `Action-URL: https://<域名>/auth/login` 与导入命令。

因此域名失效或 Cookie 过期最迟在当天（24 小时内）告警：Cookie 问题即时告警；域名发现失败在有限重试用尽后当天告警。

## 配置

见 `config/keepflame.ikuuu.example.toml`。

| 键 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- |
| `cookie_file` | 是 | — | 绝对路径，放在仓库外 |
| `nav_seeds` | 否 | `["https://ikuuu.bar/"]` | 1–10 个 https 导航页 |
| `base_url` | 否 | — | 手动指定域名（Cookie 文件内的域名优先） |
| `host_pattern` | 否 | `^ikuuu\.[a-z]{2,24}$` | 允许的主机正则 |
| `max_probes` | 否 | 8 | 1–20 |
| `timeout_seconds` | 否 | 15 | 3–60 |
| `request_pause_seconds` | 否 | 1.0 | 0–10 |

## 导出并导入 Cookie

1. 在浏览器打开告警中的登录链接（例如 `https://ikuuu.pw/auth/login`），手动登录并完成验证码。
2. 任选一种方式复制 Cookie：
   - DevTools → Network → 刷新 `/user` → 选中该请求 → Request Headers → 复制 `Cookie` 的值；或
   - Cookie-Editor 等扩展 → Export → JSON（域名会自动识别）。
3. 在 KeepFlame 主机执行（macOS）：

   ```bash
   pbpaste | keepflame ikuuu import-cookie --config /path/to/keepflame.toml --flame ikuuu-primary --base https://ikuuu.pw
   # 或：keepflame ikuuu import-cookie --flame ikuuu-primary --base https://ikuuu.pw --from-clipboard
   # 终端交互时不加管道，会以不回显方式提示粘贴
   ```

   命令把文件原子写为 mode 600，拒绝仓库内路径和符号链接，只打印 Cookie 名称与指纹，不打印值。下一次计划运行即会签到。服务在跑时可用 `keepflame run-once --config /path/to/keepflame.toml --flame ikuuu-primary --via-daemon --force --execute` 立即验收。告警里的导入命令已带上服务实际使用的 `--config` 路径。

## 更新导航种子

```bash
keepflame ikuuu discover --config /path/to/keepflame.toml --flame ikuuu-primary
```

只读抓取导航页并探测 `/auth/login`，输出候选域名、探测结果和可用域名（不签到、不发送 Cookie）。若所有种子都失效，从官方渠道（如发邮件到 find@ikuuu.pro 获取的回信、官方 Telegram）拿到新导航页地址后改 `nav_seeds`。

`discovery.EmailReplySource` 是邮件回信的可插拔接口：给它一个返回回信文本的函数即可作为额外候选来源（`IkuuuKeeper(extra_sources=...)`）；本插件不发送任何邮件。

## 已知域名（2026-10 只读核查）

- 导航页脚本解码：`ikuuu.top`、`ikuuu.pw`
- 实测登录页：`ikuuu.top`、`ikuuu.pw`、`ikuuu.org`
- 导航镜像：`ikuuu.bar`、`.de`、`.nl`、`.fyi`、`.boo`

测试夹具 `tests/fixtures/ikuuu/nav-2026-10-18.html` 是公开导航页的副本，用于锁定解码器。导航页改版导致解码失败时，发现逻辑仍会退回明文主机提取；请更新夹具和解码器。
