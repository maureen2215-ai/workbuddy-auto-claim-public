#!/usr/bin/env python3
"""Local status and preview-first pause/resume. Never deletes reward state."""
import argparse
import datetime
import json
import os
from pathlib import Path
import subprocess
import sys

DATA = Path.home() / "Library/Application Support/WorkBuddyAutoClaim"
LABEL = "local.workbuddy.autoclaim"
PLIST = Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("status", "pause", "resume"))
    parser.add_argument("--apply", action="store_true", help="明确执行暂停或恢复；恢复可能领取奖励")
    args = parser.parse_args(argv)
    if args.action != "status" and not args.apply:
        print(json.dumps({"mode": "preview", "action": args.action, "may_claim": args.action == "resume"})); return 0
    if sys.platform != "darwin": raise RuntimeError()
    domain = "gui/" + str(os.getuid()); service = domain + "/" + LABEL
    probe = subprocess.run(["/bin/launchctl", "print", service], capture_output=True, text=True)
    if args.action == "status":
        latest_path = DATA / "background-latest.json"
        latest = json.loads(latest_path.read_text()) if latest_path.exists() else {}
        print(json.dumps({"enabled": (DATA / "enabled").is_file(), "job_loaded": probe.returncode == 0,
                          "latest": {k: latest.get(k) for k in ("time", "mode", "screen_state", "results", "needs_attention", "notification")}}, ensure_ascii=False))
        return 0
    if args.action == "pause":
        enabled = DATA / "enabled"
        if enabled.exists():
            stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            os.rename(enabled, DATA / ("disabled-" + stamp))
        if probe.returncode == 0:
            subprocess.run(["/bin/launchctl", "bootout", service], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print("已暂停后续任务；已发出的请求不能撤回。程序、授权与去重记录全部保留。")
    else:
        if not PLIST.is_file(): raise RuntimeError()
        sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
        import background as b
        b.authorization(DATA / "background-authorization.json")
        os.umask(0o077)
        was_enabled = (DATA / "enabled").exists()
        (DATA / "enabled").touch(mode=0o600)
        try:
            if probe.returncode != 0:
                subprocess.run(["/bin/launchctl", "bootstrap", domain, str(PLIST)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            if not was_enabled: (DATA / "enabled").unlink(missing_ok=True)
            raise
        print("已恢复；系统按原配置检查，可能处理未领取奖励。")
    return 0


if __name__ == "__main__":
    try: sys.exit(main())
    except Exception:
        print('{"error":"LOCAL_MANAGEMENT_FAILED"}'); sys.exit(1)
