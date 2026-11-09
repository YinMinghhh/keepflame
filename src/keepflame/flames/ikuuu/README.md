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

因此当前公开登录页不能靠提交密码完成无人值守。本实现不新增密码通道。mode 600 只是文件权限，不是加密。配置里如果出现 `credential_file`，Keeper 直接判配置无效，不读取、不保存、不提交。

专用浏览器只延长本机持久性：Chrome 把带过期时间的 Cookie 留在已登记目录里，进程重启后还可以用，直到服务器原来的 `expire_in` 或站点拒绝。它不会把 `expire_in` 往后推，也不会在有 GeeTest 的页面上重新登录。会话一旦失效，就不能自动恢复，只能由人在这个专用窗口里完成验证码或二步验证。标记缺失、页面解不开、登录页读失败，都不会被当成可以提交密码。

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
3. 会话被拒绝时只读取登录文档并分类。有验证码、二步验证、邮箱验证，或文档无法解码、登录页读不到：`challenge_required`，`renewal` 为 `not_attempted`。没有这些标记同样不提交任何登录表单，结果仍是 `session_expired`。
4. 登录窗口里，POST/PUT/PATCH/DELETE 只发往配置的那个 https 源；签到 POST 在窗口里被拒绝。GeeTest 等资源主机只允许 GET。导航发现到的其他域名不会收到这个目录里的请求。
5. 目录未登记是 `profile_not_enrolled`。目录在日常 Chrome 路径下会被拒绝。目录正被占用是 `browser_busy`。浏览器无法启动是 FAILED `browser_unavailable`。
6. 同一天会再次签到，以便人在专用窗口登录后的下一次复检能变成 SUCCESS。这次复检不是自动续期。

持久目录能活过进程重启，只到服务器给出的 `expire_in` 为止，也挡不住 IP 绑定或站点撤会话。本仓库的测试用假面板和假浏览器驱动，不证明 Chrome 恢复了真实登录，也不证明站点允许完全无人值守。

## 配置

见 `config/keepflame.ikuuu.example.toml`。生产配置不在仓库里，启用浏览器通道要改服务机器上的那份 toml，而不是本示例。

| 键 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- |
| `cookie_file` | 无浏览器目录时必填 | — | 绝对路径，仓库外 |
| `browser_profile_dir` | 否 | — | 专用目录，不能是日常 Chrome 目录；设置后必须同时有 `base_url` |
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

`status --json` 只显示当前 Flame，不能回答 11 月 6 日有没有 144MB。历史用只读命令。它先读数据库里 `keep_runs` 的实际列，不迁移、不写入；`started_at` 按库存文本比较，上海早间可能落在前一日的 UTC 文本上，所以从 11 月 5 日起：

```bash
"$PY" -m keepflame ikuuu history --config "$CFG" --flame ikuuu-primary --since 2026-11-05
```

输出里的 `observed.traffic_gained` 和 `observed.checkin_state` 来自当时写进 metadata 的键。没有这些键就不会出现。含密码或 Cookie 的 metadata 只计 `secret_keys_omitted`，不打印值。

启用浏览器通道前，目录的父级必须是当前用户所有且不能被组或其他人写入。服务机器要能弹出窗口；这一点还没在 Mini 上核实。没有图形会话时，登录窗口打不开，只能继续用 Cookie 导入。

```bash
"$PY" -m keepflame ikuuu enroll-profile --config "$CFG" --flame ikuuu-primary
"$PY" -m keepflame ikuuu enroll-profile --config "$CFG" --flame ikuuu-primary --execute
"$PY" -m keepflame ikuuu login --config "$CFG" --flame ikuuu-primary
"$PY" -m keepflame ikuuu login --config "$CFG" --flame ikuuu-primary --execute
```

`login --execute` 只打开登录页并等窗口关闭。它不签到，也不读取 Cookie。窗口关闭不等于登录成功。下一轮签到 SUCCESS 才算会话可用。不要把这次 SUCCESS 说成无人值守已经验收。

Cookie 导入仍然可用。Cookie 文件的 mode 600 只限制谁能读，不是加密：

```bash
pbpaste | "$PY" -m keepflame ikuuu import-cookie --config "$CFG" --flame ikuuu-primary --base https://ikuuu.top
"$PY" -m keepflame run-once --config "$CFG" --flame ikuuu-primary --via-daemon --force --execute
```

## 已知限制

- 公开登录页有 GeeTest v4 和二步验证。专用目录不能在这种页面上续期，只能保住尚未过期的 Cookie。不能承诺无人值守，本 PR 也不是真实账户验收。
- 上游会话没有滑动续期。专用目录不能把已经过期的会话变成长效会话。
- 登录窗口的路由只放行本站、登录文档里的少量 https 静态主机，以及上述 GeeTest 主机。不放行 WebSocket。若验证码改走这里没列出的主机，窗口里完成不了，就改用 Cookie 导入。
- 自动化测试是假 HTTP、假面板或假浏览器驱动。通过不等于已经在真实账户上验收无人值守。
