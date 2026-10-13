# 贡献与维护

先读 AGENTS.md、DESIGN 和 KNOWN_ISSUES。提交最小改动；修复必须包含可复现的合成输入与对应验证。公开测试不读本机登录资料、不发实际奖励请求、不修改系统设置。

```bash
python3 -m unittest discover -s tests
node --test tests/test_desktop_session.cjs
git diff --check
```

通知源改动在本人 macOS 上编译、签名核验，并记录授权/可见性证据；CI 通过不是弹窗实测。安装器改动先用临时目录和假的 launchctl 验证，真实升级要先备份并保护当前状态。

PR 说明问题触发、修复后的行为、离线结果和仍待现场验证的内容。仅提交自己的明确文件；更新 README、AI_HANDOFF 或遗留问题中受影响的唯一权威说明，避免制造重复过时文档。

`ci/offline-tests.yml` 是可选 GitHub Actions 模板，首次发布未启用。维护者确认仓库工作流权限后可移到 `.github/workflows/`；它只运行离线测试，不需要账号凭证。手工执行上述测试也是当前有效验收入口。

增加奖励类型、跨平台、云端执行、扩大请求目标或消费资源属于新设计，不在已有授权范围中。发布前扫描全部文件与历史，版本说明写清实际证据和不确定性。
