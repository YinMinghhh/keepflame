# 聚宽签到与阅读执行协议边界

2026-10-06 从未登录的官方页面及其静态 JavaScript 只读查证；没有执行这些脚本，也未调用账号/token/验证码/签到接口。浏览器登录阶段由用户操作，`SignInGuard` 仅用于之后的自动签到执行阶段。

## 已确认的接口

| 请求 | 签到阶段上限 | 放行条件 |
| --- | --- | --- |
| GET `/view/user/floor?type=creditsdesc` | 1 | 初始主页面导航 |
| GET `/user/index/isLogin` | 2 | 仅页面初始加载；官方路由/页头登录判断 |
| GET 静态资源 | 按需 | 仅 www/cdn.joinquant.com 的 `/themes/jq/static/`，资源类型为脚本、样式、图片或字体 |
| GET `/credits/task/getTaskInfo` | 4 | 仅 Keeper 的原生 request 通道：初查加最多三次确认；页面发起的同路径请求不放行 |
| POST `/common/verifyCode/captchar` | 1 | 挑战阶段、无额外 query/body |
| POST `/common/verifyCode/validate` | 1 | 验证阶段，表单只能含本次求解的 axisX |
| GET `/user/account/getToken` | 1 | 验证通过后的签到回调准备阶段 |
| POST `/credits/task/receiveCredits` | 1 | 表单字段恰为 ruleKey、valideCode、appKey、appSecret；ruleKey 必须为 login |

其他写方法/路径、重复写请求、步骤顺序或参数变化会中止本轮。其他页面读取直接阻断，不当作最终状态证据。所有账号接口固定为 HTTPS www.joinquant.com，CDN 不允许写请求。不通过配置开放未知 endpoint。

账号接口使用 Playwright 原生 request API；写请求经 route.fetch，状态查询经 context.request.get。显式禁止重定向及传输层重试，共享剩余时间预算；响应先由 Python 观察并限制体积，再交给页面。验证码 token、签名参数、Cookie 不进入日志、结果或交接材料。原生 request 与浏览器共享本轮会话，代码不导出 Cookie。

实际签到请求绑定允许的请求对象及其完整原生响应。非 200、未知业务响应、403/429 或网络失败先中止，之后不继续状态查询。403/401 进入 WAITING_HUMAN，保留更晚的 Retry-After；429 同样保留服务端冷却。原生签到响应成功仍须最终状态接口确认。页面替换 window.fetch、返回伪造 Response 或修改 DOM 都不能代替这条请求证据。

## 公开源码依据

- [官方积分页面](https://www.joinquant.com/view/user/floor?type=creditsdesc) 指向下列构建。页面 SHA-256：`b5ab934fc0f608ae61b74c2efd9981e79b031a4e43f3d1344ae5bff1742970a5`。
- [主脚本](https://cdn.joinquant.com/themes/jq/static/dist/scripts/main.bundle.js?t=176793097500017516102590001789963951000)，SHA-256：`edfbbae90e33c4396c575408c7d1f702f84a13ae3c6b152f4007648540287e47`。路由指明用户积分组件分块；fetchSecret 的公开字面量确认 GET `/user/account/getToken` 与 appKey/appSecret 字段。只做字符串数据解码，没有执行其 JavaScript 或请求该令牌接口。
- [积分组件分块](https://cdn.joinquant.com/themes/jq/static/dist/chunk/js/chunk-824f8cab.js?fe-v=1789963951000)，SHA-256：`de672a04511bcf4c29714dc88074b0e45352cf78401fa1434d869f7ecc44e08a`。handleSignin/handleReceiveCredit 通过 receiveCredits 提交，签到规则为 login，挑战字段名为 valideCode；同一接口也服务其他积分规则，所以只限定路径不足以限定每日签到。

以上是源码证据，不是真实账号成功证据。资源/协议发生变化时保持拒绝并停止，不在已登录会话中探测任意写路径。个人资料等非必要读取被阻断可能影响页面渲染，首次真实验收若因此不兼容也应停止，不能自动放宽。

## 任务状态 envelope 与省略形式

上述同一主脚本的 HTTP 包装器从响应中取 JSON body，仅在业务码 `00000` 时返回该 body；积分组件接着读取其 `data.taskList`。因此没有证据把解析路径改为顶层 task、数组中搜索 ruleKey，或额外套一层 data。

上述同一积分组件为任务表设置 `login: {}` 默认值，再以 `Object.assign` 合并服务端 taskList。签到卡片把不存在的 `login.status` 显示为可签到，按钮调用的领取规则固定为 login；页头则只在该记录状态等于 1 时确认已签到。这构成支持省略形式的公开源码依据，不是从真实账号响应推测。

| 状态证据 | KeepFlame 判断 |
| --- | --- |
| HTTP 200、业务码 00000，data/taskList 均为对象，省略 login | eligible；保留 login_record_absent 依据，不记 SUCCESS |
| 同上，login 为对象但省略 status | eligible；保留 login_status_absent 依据，不记 SUCCESS |
| 明确整数 0 或字符串 "0" | 保留原有 eligible 判断 |
| 明确整数 1 或字符串 "1" | 已完成；这是唯一完成条件 |
| null、布尔、未知数值/字符串、数组、错误容器、缺失 data/taskList | unknown/FAILED，不默认转为 eligible |
| 403、429、认证错误等 | 先按原 HTTP/业务拒绝规则停止，不进入省略判定 |

第二轮真实验收仅保留了 HTTP 200、业务码 00000、FAILED 和次数，没有保留原响应形状。因此不能断言该账号当时正好返回了这里的某种省略形式；合成 fixture 明确取自以上公开逻辑，不冒充实录。

后续解析遇到省略或未知形式时，`status_shape` 只含 data/taskList/login/status 四个固定路径的 JSON 类型标签。`missing` 表示该键确实缺失，`unavailable` 表示父值不是对象、无法继续取该路径；标签不含任何响应值、任意键名、账号信息或 Cookie。该诊断不会发送额外请求，不能补回之前丢失的形状。下一次真实验证必须另获授权，优先只读核对此最小形状，不能凭合成 fixture 直接认定线上解析已修复。

原始状态 JSON 在解释业务码和省略形式之前检测每一层对象的重复成员。Unicode 转义解码后的同名键也算重复；无论值相同、不同或顺序如何，都拒绝整份响应，返回固定协议错误 `duplicate_json_member` / unknown，不取首值或末值、不保留部分状态，也不记录重复键名或值。不同对象里各自出现相同键仍合法。HTTP 403/429 等已有拒绝规则仍先于 JSON 解析。


## 独立阅读任务（仅合成验证）

[官方文章组件](https://cdn.joinquant.com/themes/jq/static/dist/chunk/js/chunk-bab6c78e.js?fe-v=1789963951000) SHA-256 `c2c690cea34b6f51f547312b3859c99d2a795e0120998e408d4c7a1301d08b7f`：handleBrowseTime 在 `browseTime*1000` 后经原页面 fetchSecret 发起 postView；默认 browseTime=6。公开主脚本把全局 axios POST 转换为表单。这里只允许原页面自然发起并完整校验表单，不调用该方法、不构造登记请求，也不改变计时或可见性。代码中没有直接滚动/可见性前置判断，不代表后端不校验这些条件。

同一组件 GET `/community/post/detailV2?postId=<本轮ID>` 加载文章内容；公开编辑类型表有 0–4，文章模板以 type==5 表示付费文章。KeepFlame 只接受前者，拒绝返回跳转 URL、付费类型或未知值。详情/登记响应只在内存检查，绝不记录文章作者、正文或账号信息；未知仍停止，需后续获准真实验证。

积分组件 `post_view` 卡片标题“浏览社区文章”，content“浏览1篇文章可以获得1积分，每月最多可得30分”，credit=totalCount。status 缺失选择“去看看”，0 选择“立即领取”，1 选择“已完成”；领取调用 handleReceiveCredit('post_view')。因此阅读缺失不是可领取；status=0 必须同时有非负整数 totalCount。status=1 是后端任务完成证据，但当前未确认其每日/每月业务口径，结果不虚报领取金额或当日积分。

| 阅读请求 | 每轮上限 | 条件 |
| --- | --- | --- |
| GET 配置的 `/view/community/detail/<数字>` | 1 | 初查需要阅读，持久化 pending 后；严格 HTTPS/host，无 query/fragment |
| GET `/community/post/detailV2?postId=<本轮ID>` | 1 | 阅读阶段，经原生 route.fetch、不随重定向；公开类型确认 |
| GET `/user/index/isLogin` | 文章页 2 | 阅读阶段的原页登录判断 |
| GET `/user/account/getToken` | 阅读阶段 1 | 详情已确认为受支持文章，原页面自然请求 |
| POST `/community/post/postView` | 1 | 原任务页主 frame、精确 URL、本轮 postId、表单只含 postId/appKey/appSecret、已取阅读签名 |
| GET `/view/user/floor?type=creditsdesc` | 1（门禁硬上限2） | 资格已确认后，为领取按钮加载官方积分页 |
| GET `/credits/task/getTaskInfo` 页面渲染 | 2 | 外层 floor 与内层 credit-center 各一次；仅第一份积分 document 的本任务主 frame、精确 URL、加载阶段、xhr/fetch、空 body，经原生 transport；不作为 Keeper 完成证据 |
| GET `/credits/task/getTaskInfo` Keeper 查询 | 5 | 初查1、资格后查1、最终最多3；与渲染读分开计数 |
| 挑战/答案/最终领取 | 各1 | 复用既有签到门禁，最终 ruleKey 固定 post_view；领取回调 curRuleKey/tokenType 也须匹配 |
| GET `/user/account/getToken` 领取准备 | 1 | 答案通过后的领取回调；不复用阅读签名预算 |

不允许文章页其它写操作、下载、付费、回复、点赞、任意帖子登记、额外遥测字段或 task 查询参数。其它非必要 GET 仍阻断，不能为了页面成功而扩大到任意接口。

文章组件同一公开构建的 created() 无条件调用 getAllAlgorithm()，由 axios POST `/algorithm/index/runList`，失败已由该方法的空 catch 消化。该请求不属于阅读资格或领取协议，KeepFlame 始终在本地 abort，不 fetch、不 fallback、不伪造响应。仅 reading 阶段、article_navigation=1、当前文章的原任务主 frame、精确 HTTPS 官方 URL（无 query/fragment）、空 body、xhr/fetch 的第一次 POST 可在阻断后继续本轮；次数记入 blocked_algorithm_list。重复及任何范围变化仍触发 unexpected_write，未识别的写请求也仍停止。

首次导致停止的请求以 blocked_request 保留固定枚举和布尔值，抓取发生在 phase 变为 stopped 之前。字段为 method、origin、endpoint、phase、resource_type、frame、has_query、has_fragment；endpoint 仅映射源码内审核过的公开路径，未知路径记 unknown。诊断不访问请求 header/body，不记录账号标识、原始路径、查询参数或凭据值；正常的非致命 GET/附带 POST 阻断不会抢占首个致命错误。后续请求、response 处理或页面关闭不能替换该证据。这一字段可诊断未来获准运行，不能恢复此前未记录的实录路径。

动作记录在自然阅读登记之前或领取挑战之前 reserve。pending 跨日阻断新动作；已确认当日也阻断重复尝试。未知完成返回 claim_unconfirmed，明确要求人工检查；普通恢复不清除动作记录，只有后端完成才能 confirm。所有退出路径关闭任务页再移除本轮 handler，不关闭整个账号 context。图像只在内存，Pillow 对象 finally 关闭；浏览器缓存的剩余边界不作无落盘保证。

复核补强：文章和积分页document通过原生无重定向transport获取，再交回原页面执行；静态资源底层同样不自动跟随重定向。关闭任务context所有页面成功后才撤本轮门禁，失败由browser pool停止整个自有上下文。未知应用code只记unknown；服务器时间仅保留可解析的时间或有界数字，不持久化任意返回值。

## 双组件积分入口与诊断（2026-10-09 离线修复）

同一公开积分分块（SHA-256 `de672a04511bcf4c29714dc88074b0e45352cf78401fa1434d869f7ecc44e08a`）的 module 438 外层 floor.created() 先调用自己的 fetchTaskInfo()，只更新 isSignin；module 665 内层 credit-center.created() 再调用自己的 fetchTaskInfo()，才更新 taskList。module 531 以 status 的 undefined/0/1 选择 slot，module 667 给阅读卡片提供“去看看”/“立即领取”/“已完成”。没有文案变化的缓存源码证据。

原页面渲染 GET 上限 1 会让先运行的父组件耗尽额度，子组件的请求被静默阻断，taskList.post_view 默认 {}，无法出现“立即领取”。原单 GET、直接创建按钮的 fixture 没有覆盖两个独立消费者。更新的合成页重建父子请求、初始默认 slot、Element UI 的 span/空白及父组件回调链；它不是保存的真实页面，也不执行完整官方 Vue bundle。旧代码在此 fixture 上复现 reading_button_changed、page_status=1、全部写入 0、DOM 仍显示“去看看”。2026-10-09 实测没有保存 DOM 或被阻断的第二个 GET，因此这证明代码存在该机制，不能证明它是那次实测的唯一原因。

现在仅此已知只读路径允许两次，每次原样转交原生响应，不合并、缓存重放或伪造后端状态。签到在这两次之后多出来的初始读取只阻断并计数，不停止本轮，也不计入领取。阅读任务的额外初始读取仍以 page_status_budget 停止。非本任务主 frame、非精确积分页、错误阶段或带参数/请求体不放行。领取后的页面刷新仍阻断，完成仍由 Keeper 独立原生状态确认。所有写请求的路径、参数、规则、阶段与各一次预算保持原样。403/429 等响应先停止，不重试、不追加状态请求。

reading_entry 是尽力采证，不是一次新的浏览器操作。失败后仅复制本地已经完成的安全检查：card_count、claim_count、claim_enabled；字段未出现表示没有观测到，不等于0。两个计数封顶3（表示至少3），Python校验固定键、类型和范围。不读取DOM、文本、属性或凭据，不创建CDP session、异步任务或线程，不延长任务deadline。记录 available=false 和固定原因 post_failure_read_skipped；如果本地总预算已耗尽，原因是 task_budget_exhausted。原定位失败或取消继续传播，诊断不能覆盖它，也没有新增资源等待清理。

此限制修复了早期v2诊断的退出回归：renderer在诊断开始前已卡死时，同步CDP Runtime.evaluate调用本身可能不返回，参数timeout仅约束脚本执行，不能保证协议等待的墙钟上限。旧版同反例可正常退出，v2却依赖测试监督器结束。因此不再在故障路径发起主动DOM采集，也不改成无界等待、后台线程或取消后遗留任务。已有定位超时、任务总预算、页面关闭和请求门禁仍生效；缺失DOM证据明确保留为缺口。不能用已有后端状态或局部定位观察冒充完整DOM快照。

blocked_page_status 是被阻断的已知页面状态 GET 计数，封顶100；它不能消耗或冒充Keeper的status计数。重复或禁用领取按钮在arm_challenge前拒绝。未知标签、已完成卡片、缺失资格或回调身份变化都不能触发后备点击。

门禁只在收到当前context的close事件后，才将其拦截器视为已随context销毁，不再向已关闭context重复unroute。close与unroute并发时也以这一确知事件为依据；存活context的未知清理错误仍传播。关闭监听器在所有退出路径移除，不引入额外CDP任务、线程或后台诊断。

此次修复不改变 pending、暂停、业务日期、完成回执或归档规则；零写入不授权清空 pending 或再运行。当次真实阅读仍未闭环，新的真实尝试必须单独授权，不能把本地 fixture 通过当成真实积分到账。
