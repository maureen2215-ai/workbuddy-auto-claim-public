#!/usr/bin/env python3
"""Preview-first launchd installer, with backups and rollback; no live checks by default."""
import argparse
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
DATA = Path.home() / "Library/Application Support/WorkBuddyAutoClaim"
LABEL = "local.workbuddy.autoclaim"
PLIST = Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")
NAMES = ("background.py", "desktop_session.cjs")
APP = "WorkBuddy领取提醒.app"


def launchd_config(python, runtime):
    return {"Label": LABEL, "ProgramArguments": [python, str(runtime / "background.py"), "scheduled"],
            "StartCalendarInterval": {"Hour": 6, "Minute": 30}, "StartInterval": 300,
            "RunAtLoad": True, "LimitLoadToSessionType": "Aqua", "ProcessType": "Background",
            "ExitTimeOut": 180, "StandardOutPath": str(DATA / "background-launcher.log"),
            "StandardErrorPath": str(DATA / "background-launcher-error.log")}


def run(command, **kwargs):
    return subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)


def install(python):
    if sys.platform != "darwin":
        raise RuntimeError("macOS is required")
    sys.path.insert(0, str(ROOT / "src"))
    import background as b
    if b.load_json(DATA / 'execution-owner.json', {}).get('source') == 'cloud':
        raise b.Stop('CLOUD_OWNS_SCHEDULE')
    os.umask(0o077)
    config = b.authorization(DATA / "background-authorization.json")
    if not (ROOT / APP).is_dir():
        raise RuntimeError("Build the notification app first: bash build_notifier.sh")
    run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(ROOT / APP)], check=True)
    DATA.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (DATA / "run.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if b.load_json(DATA / 'execution-owner.json', {}).get('source') == 'cloud':
            raise b.Stop('CLOUD_OWNS_SCHEDULE')
        # Installation only proceeds after a real read-only daily status check.
        headers, account = b.session(b.AUTH)
        b.Store(DATA / "background-state.json", account)
        checked, active = b.daily_status(b.API(headers).call("POST", b.STATUS))
        print(json.dumps({"preflight": "read_only_passed", "today_checked": checked, "activity_active": active}, ensure_ascii=False))
        runtime = DATA / "background"
        stamp = datetime.datetime.now(b.ZONE).strftime("%Y%m%d-%H%M%S-%f")
        backup = DATA / "backups" / ("install-" + stamp)
        backup.mkdir(mode=0o700, parents=True)
        targets = [(ROOT / "src" / name, runtime / name) for name in NAMES]
        targets.append((ROOT / APP, DATA / APP))
        snapshots = []
        for source, target in targets:
            saved = backup / target.name
            present = target.exists()
            if present:
                if target.is_dir(): shutil.copytree(target, saved, symlinks=True)
                else: shutil.copy2(target, saved)
            snapshots.append((source, target, saved, present))
        old_plist = PLIST.exists()
        if old_plist: shutil.copy2(PLIST, backup / PLIST.name)
        was_enabled = (DATA / "enabled").exists()
        domain = "gui/" + str(os.getuid()); service = domain + "/" + LABEL
        loaded = run(["/bin/launchctl", "print", service]).returncode == 0
        candidate = backup / "new.plist"
        candidate.write_bytes(plistlib.dumps(launchd_config(python, runtime)))
        run(["/usr/bin/plutil", "-lint", str(candidate)], check=True)
        if loaded: run(["/bin/launchctl", "bootout", service], check=True)
        try:
            runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
            for source, target, saved, present in snapshots:
                if source.is_dir():
                    staged = backup / "staged-reminder.app"
                    shutil.copytree(source, staged, symlinks=True)
                    if target.exists(): os.rename(target, backup / "replaced-reminder.app")
                    os.rename(staged, target)
                else:
                    staged = target.with_suffix(".next")
                    shutil.copyfile(source, staged); os.chmod(staged, 0o600); os.replace(staged, target)
            PLIST.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(candidate, PLIST); os.chmod(PLIST, 0o600)
            (DATA / "enabled").touch(mode=0o600)
            run(["/bin/launchctl", "bootstrap", domain, str(PLIST)], check=True)
        except Exception:
            run(["/bin/launchctl", "bootout", service])
            for source, target, saved, present in snapshots:
                if target.is_dir(): shutil.rmtree(target)
                elif target.exists(): target.unlink()
                if present:
                    if saved.is_dir(): shutil.copytree(saved, target, symlinks=True)
                    else: shutil.copy2(saved, target)
            if old_plist: shutil.copy2(backup / PLIST.name, PLIST)
            elif PLIST.exists(): PLIST.unlink()
            if not was_enabled and (DATA / "enabled").exists(): (DATA / "enabled").unlink()
            if loaded and old_plist: run(["/bin/launchctl", "bootstrap", domain, str(PLIST)])
            raise
        b.atomic(DATA / "background-install.json", {"time": stamp, "backup": str(backup),
                 "sha256": {name: hashlib.sha256((runtime / name).read_bytes()).hexdigest() for name in NAMES}})
        print(json.dumps({"installed": True, "backup": str(backup), "daily": "06:30 Asia/Shanghai", "poll_seconds": 300}, ensure_ascii=False))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="无参数仅预览；--install 安装并启用本机定时领取，可能立即处理未领取奖励")
    parser.add_argument("--install", action="store_true")
    args = parser.parse_args(argv)
    python = str(Path(sys.executable).resolve())
    if not args.install:
        print(json.dumps({"mode": "preview", "data_directory": str(DATA), "python": python,
                          "label": LABEL, "runtime_files": list(NAMES), "daily": "06:30 Asia/Shanghai",
                          "poll_seconds": 300, "installation_can_claim": True}, ensure_ascii=False))
        return 0
    return install(python)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        # Exception text may include account/file information; emit only known code.
        print(json.dumps({"error": getattr(error, "code", "INSTALL_FAILED"),
                          "hint": "先完成每日授权、编译通知应用和只读诊断；确认没有运行中的任务。备份保存在本机。"}, ensure_ascii=False))
        sys.exit(1)
