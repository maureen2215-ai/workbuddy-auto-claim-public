# 设计、状态与请求边界

## 运行链

`launchd → Python scheduled → 时间/启用/互斥检查 → 本机授权 → 当前登录/账号校验 → 固定官方状态查询 → 确认未领 → 保存请求意图 → 奖励请求 → 服务端回查 → 本机确认记录`

遇到故障保存经过筛选的状态码，再由独立 app 提醒。正常运行不启动 AI；加密登录使用 WorkBuddy 自带 Electron 的 Node 模式，并非依赖另一个常驻服务。

## 官方目标与最小请求集合

固定 HTTPS 主机：`copilot.tencent.com`。拒绝携带凭证跟随重定向；不关闭 TLS 检查。

| 用途 | 方法 / 路径 |
| --- | --- |
| 每日状态查询 | POST `/v2/billing/meter/checkin-activity-status`（语义只读） |
| 每日领取 | POST `/v2/billing/meter/daily-checkin` |
| 任务列表 | GET `/v2/activity/growth/tasks` |
| 已达标任务领取 | POST `/v2/activity/growth/tasks/<受限ID>/claim` |
| 旅行状态 / 地点配置 | GET `/v2/activity/growth/buddy/travel/status`、`config` |
| 旅行礼物 / 出发 | POST `/v2/activity/growth/buddy/travel/claim`、`depart` |

`API(writes=False)` 仅允许状态集合；POST 不一定是写操作，依据白名单语义区分。ID 校验拦截斜杠、路径穿越和编码穿越；真实普通句点可用。服务端业务失败即使 HTTP 200 也不是成功。

## 凭证和加密

读取客户端固定 `~/Library/Application Support/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info`，前提是本机授权已存在。兼容传统字符串 accessToken 和当前 `$wbEncrypted:1` 标准 sym-v1 字段包装；不读取/刷新 refresh token。

标准包装通过 `desktop_session.cjs` 验证 schema、base64、suite、keyId、nonce、tag 和 AES-GCM 认证。密钥由已安装 `/Applications/WorkBuddy.app/Contents/MacOS/Electron` 的本机 native provider 提供，不持久化、不输出。只把 accessToken 密文传入 helper stdin；其输出由 Python 捕获到私有管道，不能重定向进终端或日志。启动环境去掉 Node 注入参数。新 scheme、损坏密文、密钥不匹配、native provider 不可用均停止，不关闭客户端加密。

这是针对已安装客户端能力的社区兼容，依赖其内部实现，不是官方支持的 key/token SDK。未来版本可能失效，需要维护者重新只读核验和测试，而不是猜字段或提取其他账号秘密。

## 状态、去重和不确定结果

状态存在当前用户应用支持目录，包含账号摘要、日期、奖励请求意图与确认。写前原子保存 pending，写后通过真实状态/确定回执确认。崩溃或超时不立即重发；下次先回查，仍未知则 `PREVIOUS_REQUEST_UNVERIFIED` 等状态停止。明确请求未被处理的拒绝可标记 retryable。

当前登录账号摘要与已有状态不一致时 `ACCOUNT_CHANGED_REVIEW_REQUIRED`。互斥 `run.lock` 防止并发实例；跨北京时间日期时禁止当轮奖励写入。旅行有当天派出记录、免费地点指纹、每日名额和未确认礼物检查。任务回查必须 claimed，不自动接受任务。

这不是“服务器保证恰好执行一次”。客户端用意图、确认和停止策略降低重复风险；未知结果会牺牲自动继续能力，转交人工检查。

## 调度和通知

北京时间 06:30 起处理当日，系统五分钟触发，成长查询间隔三十分钟。电脑休眠时不运行；系统日历按本地时区，不同于代码固定北京时间。认证拒绝后目前一小时重试；本机读取类错误每五分钟重新检查。

正常结果安静；永久错误发 `--attention`，登录错误发 `--login`。网络连续失败满一小时才提醒。每个北京时间自然日最多成功提交一条，权限拒绝或无有效收据不算送达，一小时后可重试。回执时间为 UTC，后台事件带北京时间 `+08:00`。专注模式、系统通知设置影响可见性；QUEUED 不是用户看到通知的证明。

## 部署与隐私

安装器在本机 run.lock 下备份、暂停旧同名任务、替换源码与签名 app、注册同名任务。备份不包含运行状态的回滚覆盖步骤；保留当前状态防止重复领奖。公开仓库只保留原创源码、文档和合成测试，运行文件都在仓库外且忽略规则再次拦截。
