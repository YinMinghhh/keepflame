# ikuuu 每日签到

`keeper = "ikuuu"` 每天签到一次。默认通道仍是人工导入的会话 Cookie，`POST /user/checkin`。可选通道是运行服务的那台机器上、单独登记的 Chrome 目录。两种通道都不解验证码、不调用打码服务、不读日常 Chrome 的 Cookie 库、不拉订阅、不测速。

交互命令不要依赖 shell 里的 `keepflame`。服务入口是解释器绝对路径：

```bash
/path/to/.venv/bin/python -m keepflame …
```

LaunchAgent 已经这样启动时，裸命令 `keepflame` 在用户 shell 里找不到，是另一件事，不是服务没起来。告警里的命令用的是当时进程的 `sys.executable`。

## 会话为什么会停

上游 SSPanel-UIM 的公开 `Cookie.php`（2026-11-09 读取的 master）在登录时写入 `uid`、`email`、`key`、`ip`、`device`、`expire_in`。`expire_in` 是绝对时间，之后的请求不会把它往后推。示例配置里记住登录默认 7 天，不记住则 1 小时，并且默认绑定 IP 和 User-Agent。这是上游默认值，不是本站已核实的配置。

2026-11-09 从本环境对公开登录页做了只读 GET，没有登录、没有签到：

- `https://ikuuu.top/auth/login` 与 `https://ikuuu.pw/auth/login` 均为 200。页面是 `originBody` 包裹，解码后标题含「登录」，表单有 `email`、`password`、`remember`，以及隐藏的二步验证和邮箱验证字段，并引用 `/gt4.js`（GeeTest v4）。两个文档的哈希不同，但这些标记都在。
- 公开 `/gt4.js` 点名 `static.geetest.com`、`gcaptcha4.geetest.com`、`static.geevisit.com`、`gcaptcha4.geevisit.com`、`gcaptcha4.gsensebot.com`、`monitor.geetest.com`，文本里没有 WebSocket。
- `https://ikuuu.bar/` 在本环境是 `URLError`。这不能说明 Mini 上的导航种子不可用。

因此当前公开登录页不能靠「只提交邮箱和密码」完成无人值守。密码文件只有在登录文档没有 GeeTest、其他验证码、二步验证和邮箱验证标记时才会提交一次。今天的公开页面会停在 `challenge_required`。

## Cookie 通道

1. 读取 `cookie_file`（绝对路径、仓库外、mode 600、属主一致、非符号链接或硬链接）。文件是 `{"version":1,"base_url":"https://ikuuu.pw","cookie":"..."}`，Cookie 与导入时的域名绑定。
2. 域名发现：依次探测导入域名、`base_url`、上次成功的域名；不可用时抓取 `nav_seeds`。只有 200、最终主机不变、且页面带邮箱和密码登录表单才算可用。请求间隔 `request_pause_seconds`，最多 `max_probes` 次。
3. Cookie 只发给绑定域名。发现的新域名不同则 `WAITING_HUMAN`（`domain_changed`）。
4. 结果分类：
   - `ret == 1` 或消息为已经签到：SUCCESS。
   - 重定向路径以 `/auth/` 开头、401/403、登录页或「请登录」：`WAITING_HUMAN`（`session_expired`）。302 会记下 `redirect_path`（不要 query）。历史日志没有这个字段，不能从旧日志复原 Location。
   - 纯网络故障且 Cookie 尚未发出：FAILED，`failure_class = "transport"`。
   - 签到 POST 的网络错误仍是业务失败，不在本次运行内重发。
   - 其他失败按 `retry_minutes` 有限重试。配置无效则暂停。
5. 同一业务日、同一 Cookie 指纹的 `WAITING_HUMAN` 不再访问网络。20:00 的 `human_action_reminder` 只是再次提醒，状态仍是 `WAITING_HUMAN`，不是恢复。恢复只来自之后的 SUCCESS（`flame_recovered`）。下一次 SUCCESS 若前一条已经是 SUCCESS，运行会写入 `keep_runs`，但生命周期日志不再写一条带 `remote_status` 的 SUCCESS。所以在 out.log 里搜 `remote200` 会漏掉连续成功的日子。

## 专用浏览器通道

配置 `browser_profile_dir` 和 `base_url` 后走这条通道，不再发送 Cookie 文件。目录必须由 `enroll-profile --execute` 在服务所在机器上新建，不能指向日常 Chrome 目录。

1. 发现到的域名必须等于配置的 `base_url`。否则 `domain_changed`，不打开该目录。
2. 用该目录的持久 Chrome 发一次签到。Cookie 留在目录里，代码不调用 Cookie 导出接口。
3. 会话被拒绝时读取登录文档。有验证码或二步验证标记：`challenge_required`，不提交密码。
4. 没有这些标记且配置了 `credential_file`：提交一次邮箱、密码和页面上的 remember 字段，然后再签到一次。密码错误、响应无法归类、或提交后会话仍不可用，当天不再提交密码。
5. 没有凭据文件：`session_expired`，请在这台机器上打开登录窗口。
6. 目录未登记是 `profile_not_enrolled`。目录正被占用是 `browser_busy`。浏览器无法启动是 FAILED `browser_unavailable`，不是人工状态。
7. 同一天会再次签到，以便人刚登录后的下一次复检能变成 SUCCESS。验证码仍在时不会每小时再提交密码。

持久目录能活过进程重启，只到服务器给出的 `expire_in` 为止，也挡不住 IP 绑定或站点撤会话。本仓库的测试用假面板和假浏览器驱动，不证明 Chrome 恢复了真实登录，也不证明站点允许完全无人值守。

## 配置

见 `config/keepflame.ikuuu.example.toml`。生产配置不在仓库里，启用浏览器通道要改服务机器上的那份 toml，而不是本示例。

| 键 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- |
| `cookie_file` | 无浏览器目录时必填 | — | 绝对路径，仓库外 |
| `browser_profile_dir` | 否 | — | 专用目录；设置后必须同时有 `base_url` |
| `credential_file` | 否 | — | 仅在登录页没有验证标记时使用 |
| `nav_seeds` | 否 | `["https://ikuuu.bar/"]` | 1–10 个 https 导航页，不是登录域名 |
| `base_url` | 浏览器通道必填 | — | 登录域名，例如 `https://ikuuu.top` |
| `host_pattern` | 否 | `^ikuuu\.[a-z]{2,24}$` | 允许的主机 |
| `max_probes` | 否 | 8 | 1–20 |
| `timeout_seconds` | 否 | 15 | 3–60 |
| `request_pause_seconds` | 否 | 1.0 | 0–10 |

## 运维验收

下面的命令用 Mini 上已经在跑的解释器。把配置路径换成实际文件。不要把 Cookie、密码或 Chrome 库贴进工单。

```bash
PY=/Users/yinming/Workspace/keepflame/.venv/bin/python
CFG=/Users/yinming/Services/keepflame/acceptance.toml
"$PY" -m keepflame status --config "$CFG" --json
"$PY" -m keepflame ikuuu discover --config "$CFG" --flame ikuuu-primary
```

只读核对 11/5 之后有没有 SUCCESS，尤其是 out.log 里没有的 11/6。只取这些列：

```sql
SELECT started_at, status,
       json_extract(metadata_json,'$.reason'),
       json_extract(metadata_json,'$.remote_status'),
       json_extract(metadata_json,'$.evidence'),
       json_extract(metadata_json,'$.redirect_path'),
       json_extract(metadata_json,'$.checkin_state'),
       json_extract(metadata_json,'$.traffic_gained'),
       json_extract(metadata_json,'$.network')
FROM keep_runs
WHERE flame_id='ikuuu-primary' AND started_at >= '2026-11-05'
ORDER BY started_at;
```

启用浏览器通道前，目录的父级必须是当前用户所有且不能被组或其他人写入。服务机器要能弹出窗口；这一点还没在 Mini 上核实。没有图形会话时，登录窗口打不开，只能继续用 Cookie 导入。

```bash
"$PY" -m keepflame ikuuu enroll-profile --config "$CFG" --flame ikuuu-primary
"$PY" -m keepflame ikuuu enroll-profile --config "$CFG" --flame ikuuu-primary --execute
"$PY" -m keepflame ikuuu login --config "$CFG" --flame ikuuu-primary
"$PY" -m keepflame ikuuu login --config "$CFG" --flame ikuuu-primary --execute
```

`login --execute` 只打开登录页并等窗口关闭。它不签到，也不读取 Cookie。窗口关闭不等于登录成功。下一轮签到 SUCCESS 才算会话可用。凭据用隐藏输入写入，命令输出里没有邮箱和密码：

```bash
"$PY" -m keepflame ikuuu import-credentials --config "$CFG" --flame ikuuu-primary
```

Cookie 导入仍然可用：

```bash
pbpaste | "$PY" -m keepflame ikuuu import-cookie --config "$CFG" --flame ikuuu-primary --base https://ikuuu.top
"$PY" -m keepflame run-once --config "$CFG" --flame ikuuu-primary --via-daemon --force --execute
```

## 已知限制

- 公开登录页有 GeeTest v4 和二步验证标记。不能承诺完全无人值守。
- 上游会话没有滑动续期。专用目录不能把过期会话变成长效会话。
- 登录窗口的路由只放行本站、登录文档里的少量 https 静态主机，以及上述 GeeTest 主机。不放行 WebSocket。若验证码改走这里没列出的主机，窗口里完成不了，就改用 Cookie 导入。
- 自动化测试是假 HTTP、假面板或假浏览器驱动。通过不等于已经在真实账户上验收无人值守。
