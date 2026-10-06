# 聚宽签到执行协议与边界

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
