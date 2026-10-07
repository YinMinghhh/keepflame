# JoinQuant Flame

同一插件提供两个独立任务：`task = "checkin"`（默认）维护每日签到；`task = "reading"` 维护阅读资格及积分领取。分别配置 Flame ID，账号会话可以共享，动作预算和记录按账号+任务独立。`mode = "inspect"` 只查询；`mode = "browser"` 才进入有界浏览器动作。

## 完成依据与边界

签到以后端 `data.taskList.login.status = 1` 为完成依据。公开积分组件支持省略 login/status 作为可尝试状态，不把省略记为完成。基线 `2a478aa` 在 2026-10-06 16:54（Asia/Shanghai）单次真实签到验收成功；这不能证明长期会话、跨日调度或阅读领取已经通过真实验证。

阅读独立解释 `post_view`：条目/状态缺失表示需要阅读；明确的 0/"0" 且 totalCount 为安全非负整数表示可领取；明确的 1/"1" 表示后端报告任务完成。null、布尔、浮点状态、未知容器、重复 JSON 成员一律拒绝。不能借用签到的“省略即 eligible”。公开文案是每篇 1 分、每月最多 30 分；领取是否汇总、每日口径和真实资格门槛尚未实测，不伪造奖励金额或“今日完成”解释。

```toml
[flames.settings]
mode = "browser"
task = "reading"
article_url = "https://www.joinquant.com/view/community/detail/12345"
reading_wait_seconds = 30
# browser_profile_dir 指向运行主机本机独立登录的专用目录
```

上例仅示意插件字段，12345 是合成示例，不推荐为真实任务文章。URL 必须由使用者明确指定，精确匹配官方数字文章地址，无 query/fragment；不自动搜文。等待可配置 30–120 秒。原文章页面的定时器自然运行，程序不直接请求 postView、不伪造滚动/可见性/时间、不回放登记数据。受控的文章详情响应必须表明普通公开类型；付费类型 5、跳转或未知结构停止。6 秒只是公开组件默认计时，不是后端资格保证。

## 一轮流程与崩溃恢复

每轮先用原生请求查后端。若需阅读，在导航前持久化 pending 动作；若已可领取，在请求挑战前持久化 pending。原页面只允许为指定文章自然登记一次，之后重新查资格；未确认资格就暂停，不能继续领。领取按钮限定在唯一“浏览社区文章”卡片，最终表单规则必须是 post_view。签到仍只允许 login，不能配置任意规则。

每轮最多一个挑战、一个滑块答案和一个最终领取；图片布局、匹配或请求不明即停止。原生领取响应成功后最多后查三次，只有后端状态 1 才成功并确认动作。未确认动作跨进程、跨日保留 pending；恢复只允许只读确认，不能重置预算。失败/认证问题通过 automation_paused（认证另有 pause_account）交给核心持久暂停，禁止后台重复试。

阅读最大时间是 60 秒基础预算加配置的真实等待，签到仍 60 秒；原生账号请求上限 10 秒，禁止重定向和传输重试。阅读的后端查询最多初查1、资格后查1、完成后查3，另允许积分页自身渲染读1；渲染读不作为完成证据。更晚的 Retry-After 保留。

## 会话、图像与隐私

每轮只关闭任务页面，然后移除本轮注册的 route handler，保留 capability 所拥有的账号上下文；不能把旧门禁残留到下一轮。专用登录、长期浏览器生命周期及暂停恢复由 capability/Core 管理。关闭浏览器仍可能丢失 session cookie；不导出 cookie、不复制开发机 profile、不保证重启后免登录。真实登录、阅读或领取均须另外授权，本插件测试不要求本人操作。

拼图从响应内存解码，Pillow 图像以 finally/ExitStack 关闭，求解后的图像 URI 引用及时释放；没有真实图片文件或调试截图。浏览器自己的缓存、崩溃日志和 profile 不因此保证完全无落盘；不删除既存用户数据以制造该保证。

不保存账号响应全文、任意字段、token、签名参数或 cookie。结果只保留固定状态/类型、阶段计数、错误码及任务枚举。首次导致本轮停止的页面请求另以 `blocked_request` 保存固定标签：方法、origin 类别、已知 endpoint、发生阶段、资源类型、frame 关系及 query/fragment 存在与否；未知值归为 other/unknown，不保存 URL、body 或 header。后续请求及清理不能覆盖首次失败证据，CLI JSON、持久结果和 JSONL 通知保留该字段。配置/Profile 必须留在仓库和发布包之外；邮件功能本轮暂停。

官方文章初始化的 `POST /algorithm/index/runList` 是一个精确的本地阻断例外：阅读阶段、首次文章导航后的原任务主 frame、精确官方 URL、空 POST、xhr/fetch、每轮最多一次时，只 abort 并增加 `stage_counts.blocked_algorithm_list`，不终止阅读。请求仍不发给服务器，也不返回伪造成功；重复、参数/来源/阶段变化及其他未知写请求继续停止。该计数与 `blocked_request` 不同：前者是容忍的本地阻断，后者是导致停机的首个请求。阅读资格及领取规则保持不变，现有 pending 不会被修复清除。

## 验证

默认测试完全离线，使用合成状态、内存存储和图片。选择性 Chrome 测试使用临时空 profile，所有请求由进程内 route.fulfill 或内存 transport 响应，DNS 禁止外部解析，不连接聚宽或本机其他应用端口。

```bash
.venv/bin/python -m pytest
KEEPFLAME_SYNTHETIC_BROWSER=1 .venv/bin/python -m pytest tests/test_joinquant_reading_browser.py tests/test_joinquant_browser.py
```

阅读目前仅公开源码依据+离线/合成 Chrome 验证，未使用真实账号观察资格或领取。官方 DOM/回调变化时停止，不临时放宽门禁。平台协议限制未经授权第三方接入，技术实现不代表平台许可。精确接口与公开依据见 [PROTOCOL.md](PROTOCOL.md)。
