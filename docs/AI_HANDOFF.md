# 给没有上下文的 AI：接手与复用

## 目标和正本

这是社区 macOS 本机自动领奖脚本。核心是 `src/background.py`；`src/desktop_session.cjs` 兼容当前标准加密登录；`src/notifier.m` 负责本机通知。程序访问 `https://copilot.tencent.com` 的固定状态/奖励接口。它不是官方公开签到 SDK，不保证接口稳定。

本仓库源码是开发正本。实际运行副本位于当前用户 `~/Library/Application Support/WorkBuddyAutoClaim/background/`，通知 app 在同一应用支持目录；修改仓库不自动修改运行副本。launchd 标签 `local.workbuddy.autoclaim`。依据实际 `git status`、文件摘要、plist 和日志判断版本，不依赖旧聊天或维护者电脑路径。

## 首次接手：先报告，后部署

1. 读 README、INSTALL，确认用户需求是每日领取、旅行还是任务奖励；初次仅推荐每日领取。报告计划改变的本机文件、账号动作及停止点。完成标准：用户能清楚判断授权范围。
2. 只读核验 macOS、Python、WorkBuddy 版本与安装位置，是否存在相同 label/数据目录，Git 是否有未提交修改。不要读取/打印整个登录文件。完成标准：确定是新安装还是维护已有程序，已有记录已识别。
3. 离线执行 `python3 -m unittest discover -s tests` 和 `node --test tests/test_desktop_session.cjs`；安装器与配置工具无参数只是预览。完成标准：测试全过或准确列出未满足依赖。
4. 用户明确允许使用本人客户端登录状态、仅访问官方目标并领取指定奖励后，按 INSTALL 创建每日授权。配置工具拒绝覆盖已有授权；已有项目由维护流程接管。完成标准：真实授权范围可核对，凭证不落仓库。
5. 运行 `diagnose` 核实服务端状态；它禁用奖励写接口，可能记本机诊断错误。完成标准：明确有效布尔字段，不靠按钮消失/HTTP 200 推测。
6. 按 INSTALL 编译、预览、安装，并由本人允许通知。完成标准：实际 Python 路径、已部署的两个源文件、签名 app、任务标签和时区均可核对。
7. 等系统自然运行再检查；逐项记录当前定时、下一天、锁屏时真实结果。完成标准：有 `scheduled` 和服务端确认，客户端到账由本人核对。用户手领用 `DAILY_ALREADY_VERIFIED`，不能归给脚本。

若用户一开始已明确授权整个上述范围，不重复询问同一授权；系统密码与通知权限仍由本人处理。请求旅行前，本人需核实免费地点，保存活的配置指纹。只读诊断不是“所有私密文件可读取”的授权。

## 日常健康检查

先执行 `manage.py status`。从本机 `background-events.jsonl`（及轮换副本）按北京时间逐日统计成功与异常，结合 `background-state.json` 的 daily_confirmed。不要把这些原始文件提交公开问题。缺历史日志标未知，不能用单日结果推断连续成功。

`launchctl print gui/<当前UID>/local.workbuddy.autoclaim` 查看实际入口、运行次数、退出码和日历/轮询配置。`not running` 是短任务正常空闲。无异常无需重装、手动 run 或变更设置。

只有用户请求/原授权范围下，才做服务端只读 `diagnose`；需要执行实际奖励时，先明确 `run`/`scheduled` 会写入。对过期日期不补登。

## 排错与修改

先按 KNOWN_ISSUES 判定问题属于调度、登录、协议、奖励状态或通知。保留原日志与去重记录；`PREVIOUS_REQUEST_UNVERIFIED` 先回查，不能删 pending。账号变化停止，不能把旧账号的记录改名套用。

修改 API 白名单、去重、认证、加密或状态流程前读 DESIGN，增加能发现真实风险的合成测试。令牌只留进程内存，密文通过 helper stdin，令牌通过其私有 stdout 管道返回 Python；不要独立执行 helper、打印输出或把异常正文送进日志。未知 schema/scheme 以状态码停止。

通知更新涉及签名；不要无故覆盖已授权 app。部署前取得互斥锁，备份程序与 plist，保留状态和授权，部署后比对摘要。仅源码改动不得宣布已上线。

## 可交付的报告格式

用普通语言告诉用户：今天是否真的领了、时间和分数；是脚本领的还是用户手领；当前阻塞；改了什么；哪些是测试、哪些是现场；下一步最小动作。把“待锁屏/跨日实测、待用户通知可见性确认”写成明确待办，不包装成已完成。

长期维护保留 README 的能力边界、KNOWN_ISSUES 的待办和 VERIFICATION 的事实证据。公开发布前检查全部提交文件与历史，源码之外的账号数据不上传。
