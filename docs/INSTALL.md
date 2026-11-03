# 安装与首次验收

本页适用于 Mac 本机模式。希望电脑关机时仍领取，请按 [云端部署与切换](CLOUD.md) 操作。同一账号不同时启用两个领取器。

仅针对自己的 Mac 和本人 WorkBuddy 账号。默认每日积分，可选能力另行授权。下面所有路径都相对于克隆后的仓库；运行数据在本人 `~/Library/Application Support/WorkBuddyAutoClaim/`。

## 1. 检查环境和已有任务

确认 macOS 13+、Python 3.10+、WorkBuddy 已安装到 `/Applications/WorkBuddy.app` 并已登录。克隆仓库和离线测试不需要读凭证。

```bash
python3 --version
python3 -m unittest discover -s tests
node --test tests/test_desktop_session.cjs
python3 configure.py
python3 install_background.py
python3 manage.py status
```

Node 测试在 Node.js 18+ 下运行。没有 Node 可先完成 Python 测试，维护加密组件时必须补做 Node 测试。Apple Silicon 尚无维护者真机验收，按新设备处理。

如果已有 `local.workbuddy.autoclaim` 或同名数据目录，先接管原状态并查运行入口；不要用清空记录的方法做全新安装。配置工具遇到已有授权不会覆盖。新仓库与原始单机版使用同一个任务标签，避免同时运行两个领取器。

## 2. 明确授权每日积分

这一步会保存本机授权，只允许使用当前客户端登录状态访问固定官方服务和每日领取，不安装定时器、不发奖励写请求：

```bash
python3 configure.py --authorize-daily
python3 src/background.py diagnose
```

`diagnose` 成功应返回 `checks.daily`，包括真实布尔 `checked`、`active`。它禁止奖励写接口，但错误诊断可能写本机错误记录。不要输出登录文件、token 或完整 HTTP 报文。

出现错误时按 `docs/KNOWN_ISSUES.md` 排查；未通过只读查询就停止安装。客户端加密兼容只支持已验证的标准 sym-v1 包装，不支持未知 scheme，不修改客户端或关闭加密。

## 3. 编译通知应用

```bash
bash build_notifier.sh
```

缺少 `clang` 时，需要本人安装 Apple Command Line Tools；AI 先说明这项系统安装，再由本人确认。不要打包别人的已签名二进制。

安装器要求通知 app 已构建且签名核验通过，会把它一并复制到应用支持目录。系统通知授权由本人完成，编译/安装不等于已授权。

## 4. 预览并安装

```bash
python3 install_background.py
python3 install_background.py --install
```

第二条会：只读检查账号和每日活动；取得原程序的互斥锁；备份旧 Python、兼容组件、通知 app 和 plist；复制运行副本；保存 `enabled`；注册/替换当前用户的 `local.workbuddy.autoclaim`。**安装会启用实际奖励处理，可能在安装后立即或下一轮执行。** 失败会尝试恢复本轮替换前的文件和任务，运行状态和授权不删除。

它不改网络、电源、系统解锁或 WorkBuddy 软件。`StartInterval=300`；成长中心内部查询间隔 1800 秒；业务日期固定北京时间。日历的 06:30 取系统时区，其他时区可由轮询在北京时间 06:30 后补查，不能承诺秒级准时。

## 5. 允许通知并试一次

```bash
open -g -W -n -a "$HOME/Library/Application Support/WorkBuddyAutoClaim/WorkBuddy领取提醒.app" --args --setup setup-local
```

本人允许通知，并在 macOS 系统设置中启用横幅/提醒、通知中心及按需要显示锁屏通知。若曾拒绝授权，通过系统设置修改，AI 不代输系统密码。

```bash
open -g -W -n -a "$HOME/Library/Application Support/WorkBuddyAutoClaim/WorkBuddy领取提醒.app" --args --status status-local
open -g -W -n -a "$HOME/Library/Application Support/WorkBuddyAutoClaim/WorkBuddy领取提醒.app" --args --test test-local
```

回执 `notification-result.json` 的 `QUEUED` 只代表系统接受，不等于看到横幅。由本人确认“看到了、点击打开了 WorkBuddy”。通知代码更新重签后重新核实授权。

## 6. 实际验收

```bash
python3 manage.py status
```

核对本机事件中 `mode=scheduled` 和最近时间。当天已手动领取，应看到 `DAILY_ALREADY_VERIFIED`，不能算作脚本新增积分。未领取时，只有 `DAILY_CLAIM_VERIFIED` 与写后状态确认才算脚本领取成功。

验收分三步记录：安装后实际定时运行、下一天自动领取、本人锁屏且保持唤醒时的实际运行。没有现场记录就标“待验证”。`not running` 是正常空闲；退出码 0 本身不证明到账。最终与本人客户端到账情况相互核对。

## 7. 可选奖励

每日流程验证后再选择；这些配置操作本身不发奖励写请求，但已经安装的定时器会在后续轮次执行新范围。

```bash
python3 configure.py --review-travel
```

在 WorkBuddy 界面确认选中的地点每天可用且不消耗积分或能量。服务端没给费用字段不能单独证明免费。替换下面 ID 为刚核对的值，不能机械照抄示例：

```bash
python3 configure.py --enable-travel 1 --confirm-free
python3 configure.py --enable-completed-tasks
```

地点指纹保存于本机配置，后续变化停止派出并要求复核。任务只领明确 completed、locked=false 的奖励，不自动 accept，不做任务进度。若想减少范围，先暂停，再由维护者核对并修改本机对应布尔配置，保存备份；没有批量清理状态步骤。

## 暂停、恢复和回滚

```bash
python3 manage.py pause --apply
python3 manage.py resume --apply
```

暂停保留记录，已经发出的请求不能撤回。恢复沿用原 plist，并可能开始领取。

回滚先暂停后续任务，在本机 `backups/install-*` 选定对应备份，恢复 Python、兼容组件、签名通知 app 和 plist，再核实其版本/授权后恢复。保留当前去重状态，不覆盖为旧状态快照；新旧版本状态不兼容时停止，请维护者评估。不能恢复已被服务器领取的奖励，也不能凭空补领过去日期。
