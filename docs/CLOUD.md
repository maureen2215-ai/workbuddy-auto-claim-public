# Linux 云端：安装、凭证更新与回退

适合希望 Mac 关机后仍自动领取、并拥有自己控制的 Linux 服务器的用户。首次授权目前仍从已登录 WorkBuddy 的 Mac 进行；不支持直接在 Linux 完成 WorkBuddy 登录，也没有实现自动刷新登录凭证。

本页中的 `YOUR_SSH_ALIAS` 是你自己的 SSH 目标占位符，不是可直接使用的服务器。

## 先检查条件与授权

- 本地：macOS、Python 3.10+、WorkBuddy 已登录、SSH 客户端；先配置并核验服务器主机密钥，脚本强制 StrictHostKeyChecking，不代替用户信任陌生主机。
- 服务器：Linux、Python 3.10+、systemd 249+、`useradd`、可用的 TLS 根证书、无交互 sudo。已实测运行环境为 Linux x86_64、Python 3.12、systemd 255。工具不安装系统依赖、不购买服务器。
- 选择一个领取执行端。现有本机任务在 stage 阶段继续运行，activate 才切换；另有自建 cron/其他账号/历史脚本时，必须先人工盘点并停用重复执行者。
- 用户需明确同意：登录凭证离开 Mac 并长期存放所选服务器，服务器管理员有能力访问；不上传 GitHub、日志平台或第三方签到服务。

凭证源文件保留在 Mac，云端只接收必要的请求头和固定账号指纹，保存于 root-owned 0600 的 `/etc/workbuddy-auto-claim/session.json`。它是受文件权限保护的凭证文件，不宣称已加密存储；运行时通过 systemd LoadCredential 交给独立服务用户。进程禁止 core dump，凭证不出现在参数、环境值和日志里。

## 1. 本地预览与每日积分授权

```bash
python3 cloud/deploy.py
python3 configure.py
python3 configure.py --authorize-daily
```

已有本机授权时保留它，不重复创建。新用户默认只授权每日积分；旅行、已完成任务领奖按照本机安装文档独立预览并授权。云端 stage 会复制当时授权范围，之后修改本机授权不会自动改变云端范围。

## 2. 暂存并验证，不启动领取

在用户确认目标和凭证保存范围之后运行：

```bash
python3 cloud/deploy.py --host YOUR_SSH_ALIAS --stage --authorize-cloud-session
```

脚本在本机锁内读取登录，核对账号及官方状态，经 SSH 私有标准输入传输。服务器新建专用无登录权限用户、代码/状态/凭证目录和 systemd 单元，启动一次只读 verify 服务。成功输出必须为 `INSTALLED_TIMER_NOT_ENABLED`，且 verification 含本次时间和布尔状态。

已有同名云端目录、服务用户、systemd 单元或本机 cloud-control 配置时停止，避免覆盖其他安装。它是首次安装器，不是升级器。远端安装失败会尝试停止本次只读服务，并清理本次新建对象，返回 rollback_complete；不会清理预先存在的服务。本机在上传前保存目标指针和阶段回执，即使传输中断也能定位检查。回执不明或 rollback_complete=false 时先只读检查；保留本机指针用于接管，确认失败安装已恢复后再人工归档它以重试，不能直接删除远端目录强行重装。

完成标准：远端可读取保存的凭证，账号匹配，只读查询成功，领取 timer 尚未启用；本地不保存解密后的凭证。

## 3. 切换为云端执行

```bash
python3 cloud/deploy.py --activate
python3 cloud/cloud_control.py status
python3 cloud/cloud_control.py verify
```

activate 取得本地锁，备份最终账本和旧 LaunchAgent，disable/bootout 本机领取并移走 enabled，写 execution-owner.json 标记，迁移最终账本后才 enable 云端 timer。中途失败不会自动恢复本机领取，必须先确认云端是否已运行，避免双写。

每日北京时间 06:30 起处理；timer 每五分钟触发，06:30 前程序跳过网络领奖。已经核实当天已领时不重复；成长中心约三十分钟检查。断网后补查当天，不补领过去日期。systemd 定时器不依赖 Mac、Codex 或 AI 模型。

完成标准：本机旧任务不在运行且启用标记已移走；云端 timer enabled/active，实际 service 入口运行正常；当天服务端状态与事件对应。若当天已领，只能验收去重，须在下一次未领取日观察 `DAILY_CLAIM_VERIFIED` 才能确认新领取。

## 4. 可选：Mac 失效提醒

```bash
bash build_notifier.sh
python3 cloud/deploy.py --install-monitor
```

已经安装监测器或已有通知 app 时保留，不盲目覆盖。新安装用户按本机安装文档给「WorkBuddy领取提醒」开启通知、执行测试并本人确认横幅可见。

监测器登录加载，每三十分钟只读查看云端；登录失效、timer 停止、运行失败、心跳过旧或早上仍未确认领取时，通过现有 Mac 通知提醒。短暂 SSH 不可达先观察，持续一小时才提醒；同日通知有去重。Mac 关机、断网或休眠时提醒会延后。**未配置云端直达手机、邮件或飞书通知。** 用户如果需要，先确认通知渠道与收件人，再单独接入；不要擅自发消息。

## 5. 凭证失效后更新

用户在 WorkBuddy 完成登录后：

```bash
python3 cloud/cloud_control.py refresh
python3 cloud/cloud_control.py verify
```

refresh 会再次通过加密 SSH 传输凭证。远端先匹配固定账号/租户并只读验证候选凭证，再原子替换 root-only 文件；不会启用新的领取器。登录凭证寿命未知，仅打开应用不保证登录更新。账号变更立即停止，不能通过清空状态或随意改 expected_account 绕过。

更新成功与真实过期恢复是不同证据层级；目前核心云端领取已实测，未来自然过期的完整提醒与更新流程仍待验收。

## 维护与受控回退

云端代码位于 `/opt/workbuddy-auto-claim/`，状态位于 `/var/lib/workbuddy-auto-claim/`，授权与凭证位于 `/etc/workbuddy-auto-claim/`。专用用户 `workbuddy-claim`，最多 96 MiB 内存、10% CPU，单轮 180 秒超时，不开公网端口。

暂停或回退时，先在用户服务器执行：

```bash
sudo systemctl disable --now workbuddy-autoclaim.timer
sudo systemctl is-active workbuddy-autoclaim.service
```

如果 service 仍 active/activating，等待它结束并检查结果；停止 timer 不等于停止当前 service。保留现有云端备份，取回云端最新账本，核对账户一致，再在本地运行锁内替换账本。完成后才归档 execution-owner.json 标记，恢复 enabled、enable/bootstrap 本机 LaunchAgent。不要用切换前旧账本回退；它不知道后续云端已执行的请求。若回退至新版公开本机安装器，可在确认远端停止且账本已同步后重新安装；须先处理 launchctl disable 的持久禁用状态。

不自动清理云端凭证与目录；明确卸载请求另行处理。故障时不改 VPN、DNS、代理或其他同机服务。

## 给没有上下文的 AI

先读 AGENTS.md、本文及 VERIFICATION.md。确认用户选择本机还是云端；核对已存在的执行端、服务器所有权、SSH 主机密钥、sudo、Python/systemd 和凭证保存授权。按上述阶段验证，任何一步失败保持单一执行端；最终报告真实领取、只读核验和仅离线测试的区别。凭证、私有服务器地址、账号指纹、原始状态和日志都不能进入公开提交或 issue。
