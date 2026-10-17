# JoinQuant Flame

同一插件提供两个独立任务：`task = "checkin"`（默认）维护每日签到；`task = "reading"` 维护阅读资格及积分领取。分别配置 Flame ID，账号会话可以共享，动作预算和记录按账号+任务独立。`mode = "inspect"` 只查询；`mode = "browser"` 才进入有界浏览器动作。

## 完成依据与边界

签到以后端 `data.taskList.login.status = 1` 为完成依据。公开积分组件支持省略 login/status 作为可尝试状态，不把省略记为完成。基线 `2a478aa` 在 2026-10-06 16:54（Asia/Shanghai）单次真实签到验收成功；这不能证明长期会话、跨日调度或阅读领取已经通过真实验证。

阅读独立解释 `post_view`：条目/状态缺失表示需要阅读；明确的 0/"0" 且 totalCount 为安全非负整数表示可领取；明确的 1/"1" 表示后端报告任务完成。null、布尔、浮点状态、未知容器、重复 JSON 成员一律拒绝。不能借用签到的“省略即 eligible”。公开文案是每篇 1 分、每月最多 30 分；领取是否汇总、每日口径和真实资格门槛尚未实测，不伪造奖励金额或“今日完成”解释。

```toml
[flames.settings]
mode = "browser"
task = "reading"
reading_discovery = true           # 自动选文（默认且推荐）
reading_discovery_max_requests = 30  # 5–60
reading_wait_seconds = 50          # 30–120，默认 50
reading_scroll = true              # 等待中一次缓慢向下滚动
# 可选、不推荐：固定文章列表，优先于自动发现，读完后再自动发现
# article_urls = ["https://www.joinquant.com/view/community/detail/12345"]
# browser_profile_dir 指向运行主机本机独立登录的专用目录
```

上例仅示意插件字段，12345 是合成示例，不推荐为真实任务文章。URL 必须由使用者明确指定，精确匹配官方数字文章地址，无 query/fragment；不自动搜文。等待可配置 30–120 秒。原文章页面的定时器自然运行，程序不直接请求 postView、不伪造滚动/可见性/时间、不回放登记数据。受控的文章详情响应必须表明普通公开类型；付费类型 5、跳转或未知结构停止。6 秒只是公开组件默认计时，不是后端资格保证。

## 一轮流程与崩溃恢复

每轮先用原生请求查后端。若需阅读，在导航前持久化 pending 动作；若已可领取，在请求挑战前持久化 pending。原页面只允许为指定文章自然登记一次，之后重新查资格；未确认资格就暂停，不能继续领。领取按钮限定在唯一“浏览社区文章”卡片，最终表单规则必须是 post_view。签到仍只允许 login，不能配置任意规则。

每轮最多一个挑战、一个滑块答案和一个最终领取；图片布局、匹配或请求不明即停止。原生领取响应成功后最多后查三次，只有后端状态 1 才成功并确认动作。未确认动作跨进程、跨日保留 pending；恢复只允许只读确认，不能重置预算。已提交领取或认证问题通过 automation_paused（认证另有 pause_account）交给核心持久暂停，禁止后台重复试。本轮领取计数恰好为整数 0、且该 pending 没有归档时，不写 automation_paused，下一业务日仍可再查。已归档的 pending 保持暂停。

阅读最大时间是 60 秒基础预算加配置的真实等待，签到仍 60 秒；原生账号请求上限 10 秒，禁止重定向和传输重试。阅读的后端查询最多初查1、资格后查1、完成后查3，另允许积分页自身渲染读1；渲染读不作为完成证据。更晚的 Retry-After 保留。

## 会话、图像与隐私

每轮只关闭任务页面，然后移除本轮注册的 route handler，保留 capability 所拥有的账号上下文；不能把旧门禁残留到下一轮。专用登录、长期浏览器生命周期及暂停恢复由 capability/Core 管理。人工登录窗口放行官方登录验证码 `/common/verifyCode/`（登录滑块、图形码和短信），否则登录按钮会停在验证码请求上且页面没有错误提示。同一窗口仍阻断 `/credits/`、`/user/account/getToken` 和 `/community/post/postView`，不能在登录过程中领取积分或登记阅读。关闭浏览器后，该 profile 会留下聚宽响应中没有有效期的 HttpOnly 会话 Cookie，并只在这个目录里补上有限有效期；下一个使用同一 profile 的进程会在同源请求中带上它。Cookie 值不进入日志、profile 以外的文件或其他账号 profile，也不复制开发机 profile。远端返回 10001 时仍是等待人工，本地保留不是登录成功。真实登录、阅读或领取均须另外授权，本插件测试不要求本人操作。

拼图从响应内存解码，Pillow 图像以 finally/ExitStack 关闭，求解后的图像 URI 引用及时释放；没有真实图片文件或调试截图。浏览器自己的缓存、崩溃日志和 profile 不因此保证完全无落盘；不删除既存用户数据以制造该保证。

不保存账号响应全文、任意字段、token、签名参数或 cookie。结果只保留固定状态/类型、阶段计数、错误码及任务枚举。首次导致本轮停止的页面请求另以 `blocked_request` 保存固定标签：方法、origin 类别、已知 endpoint、发生阶段、资源类型、frame 关系及 query/fragment 存在与否，另记官方 origin 的脱敏裸路径 `path`（纯数字段为 `:id`，含数字混排等其他段为 `:x`）；未知值归为 other/unknown，不保存完整 URL、query、body 或 header。后续请求及清理不能覆盖首次失败证据，CLI JSON、持久结果和 JSONL 通知保留该字段。配置/Profile 必须留在仓库和发布包之外。邮箱通知由 notifier 投递，默认关闭，不连接真实邮箱。

官方文章初始化的 `POST /algorithm/index/runList` 是一个精确的本地阻断例外：阅读阶段、首次文章导航后的原任务主 frame、精确官方 URL、空 POST、xhr/fetch、每轮最多一次时，只 abort 并增加 `stage_counts.blocked_algorithm_list`，不终止阅读。请求仍不发给服务器，也不返回伪造成功；重复、参数/来源/阶段变化及其他未知写请求继续停止。该计数与 `blocked_request` 不同：前者是容忍的本地阻断，后者是导致停机的首个请求。阅读资格及领取规则保持不变，现有 pending 不会被修复清除。

## 验证

默认测试完全离线，使用合成状态、内存存储和图片。选择性 Chrome 测试使用临时空 profile，所有请求由进程内 route.fulfill 或内存 transport 响应，DNS 禁止外部解析，不连接聚宽或本机其他应用端口。

```bash
.venv/bin/python -m pytest
KEEPFLAME_SYNTHETIC_BROWSER=1 .venv/bin/python -m pytest tests/test_joinquant_reading_browser.py tests/test_joinquant_browser.py
```

阅读目前仅公开源码依据+离线/合成 Chrome 验证，未使用真实账号观察资格或领取。官方 DOM/回调变化时停止，不临时放宽门禁。平台协议限制未经授权第三方接入，技术实现不代表平台许可。精确接口与公开依据见 [PROTOCOL.md](PROTOCOL.md)。

当前日状态不能证明其他日期的未决动作完成。已有 pending 的 day 与本轮北京时间业务日不一致时，即使观察到 complete，也保留观察元数据并返回 WAITING_HUMAN / pending_different_day，继续暂停；不读取文章、不挑战、不领取、不确认旧动作。只有同日且存储原子确认成功才收敛该 pending；存储拒绝则以 action_confirmation_rejected 停止。例外：该 pending 关联运行的 stage_counts.claim 恰好是整数 0，且没有归档证书时，下一个 Asia/Shanghai 业务日可以重新预留，原行换成新的 pending，不删除历史。若当天官方状态已经完成，先把该 pending 预留到当天再确认。已提交领取、同一天、缺少该计数或已有归档的 pending 仍停在原日并继续暂停。

签到在离开加载阶段前等待官方页面自己的两次 task-info 读取；这两次读取只在加载阶段放行。随后只在页面上恰好有一个「签到」按钮时点击一次。签到多出来的 task-info 读取只记入 blocked_page_status，不停止这一轮，也不算领取。阅读任务的第三次初始读取仍以 page_status_budget 停止。没有发出挑战、答案或领取、只因按钮未出现而失败的签到，按核心的有限重试阶梯在同一上海日再次到期（`same_day_retry_minutes` 只是下限），不暂停账号。非人工停止不再自带六小时延迟，只保留平台 `Retry-After` 下限；已经发出挑战、答案或领取的失败靠暂停保持停止，未决写入不会改到新的一天。

聚宽浏览器任务使用可见窗口。browser_headless = true 不会隐藏这个窗口，结果 metadata 的 browser_window 为 visible。官方积分流水出现本轮任务的新行时，结果记录该行时间、项目原文和数量，奖励单位是积分、来源是观察到的值。流水缺失、code 20000 或没有新增时，金额记为未证实，不编造数量，也不构造奖励。

运行中业务日期由 Core 的实时 clock 提供，固定为本轮开始的上海日期。每次外部状态读取、耗时阅读、挑战、求解、提交前后以及最终确认都校验日期；请求门禁在每个页面请求和原生响应处也校验，HTTP Date 存在时必须同日。日期变化或时钟退回其他日期后锁定 business_day_changed，即使时间跳回也不重试，旧 intent 保持 pending。SQLite 事务中的日期检查失败会回滚。独立构造的 Context 未提供 clock 时以开始时间加单调时钟经过时间推进；生产 Orchestrator 始终传入实时 clock。

归档证据审查仅接受失败/等待人工的同一运行记录、受支持守卫计数版本，以及登记/挑战/答案/领取四个整数计数全0，并限制为已知的停止原因。它只能证明这些守卫写请求未获放行，不能证明奖励成功或修复缺失的首拒路径。新记录携带 write_guard_schema=1；旧 acbe79b 的计数语义已逐源审查，须显式引用该完整source revision及证据SHA256。哈希是人工/代码审阅材料的定位依据，不是假定任意文件自带真实性证明。缺失计数、非0、布尔计数、其他旧源码或未知原因只能归档unknown并继续阻塞。

abandoned 不自动恢复 Flame、不改变启用开关，也不跳过下一日的实时只读资格、门禁和单轮写预算。之前发送过或是否发送不明的动作不能通过此流程获得新预算。不得把昨日尚未用掉的一次真实验收授权自动扩展到新日期。

`reading_button_changed` 不是通用归档白名单。`archive_cases.py` 只固定一个已审计历史案例：完整 action/run 原始行的 SHA256、执行源码及精确证据集合必须全部一致；证书还绑定入口修复候选及其独审材料。它同时要求 schema=1 和登记/挑战/答案/领取四个严格整数零计数。任意其他日期、账号、intent、运行或仅有相同错误/四零计数都拒绝 abandoned；证据不足仍按 unknown 保留阻塞。摘要用于绑定已审阅材料，不是任意文件的真实性证明或真实操作授权。归档仍须过期业务日、追加不可改审计，保留原 pending 行和暂停；不修改数据库 schema，也不放行同日重试。新增案例必须另做源码/证据审查，不允许配置扩白名单。

所有 JoinQuant abandoned 审查入口先按预留身份（keeper/account/task/day/started_at）或 run UUID 的固定摘要识别该案例，再解释 reason；任一身份命中都必须通过完整原始行和证据校验。引用该案例的专属证书也强制进入完整校验，不能把它移用于另一个旧通用案例。修改 reason 为旧白名单值、改变状态或替换证据不能退回旧通用分支。与该 intent、run 和专属证书均无关的合法普通案例仍按原规则审查，unknown 仍不释放预算。

业务时区声明固定为 Asia/Shanghai，与 Flame 调度不一致时入口停止。完成后查和动作事务确认成功时即返回本轮 Core 签发的成功回执；回执保留原目标日期和观察时刻，随后 close 或上下文退出跨过午夜不能把它记作次日成功。清理实际抛错时保留成功回执，同时因资源问题暂停；仅日期变化不会在已完成后改成 unknown 或要求登录。确认前仍逐阶段执行实时日期围栏，SQL 确认中日期改变仍回滚，不放宽写请求守卫。

## 自动选文（默认）与可选文章列表

默认不配置文章，只靠下面的自动发现。旧的 `article_url`/`article_urls` 仍兼容但不推荐：配置时每天先选其中第一篇从未读过的文章；`postView` 一旦发出，或文章被判为付费/未知类型，即写入 `reading_articles`，以后不再选它。

没有可用的配置文章且 `reading_discovery = true`（默认）时，只在当天确实需要阅读时才自动发现：用不带 Cookie、不跟随跳转的匿名 `GET https://www.joinquant.com/post/<数字ID>` 读取跳转目标。跳到 `/view/community/detail/<32位哈希>` 且无 query 的视为普通文章；带 `?type=` 的问答帖、404 和未知目标都跳过。先从缓存的最大 ID（`reading_discovery`，首次默认 81500）向上倍增、再二分估计当前最大 ID，然后向回找第一篇未读文章。请求数受 `reading_discovery_max_requests` 限制，间隔 0.5 秒。发现不经过账号浏览器和写门禁，也不登记或领取；选中的文章仍走原有的类型检查与阅读流程。网络失败停在 `reading_discovery_unavailable`、找不到停在 `reading_articles_exhausted`，均需要人工处理并通知，不重复读旧文章。
