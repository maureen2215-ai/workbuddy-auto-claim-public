#!/usr/bin/env python3
"""Explicit local authorization. Defaults to a preview; never claims rewards."""
import argparse
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
import background as b


def daily_config():
    return {"use_desktop_session": True, "endpoint": b.BASE, "daily": True,
            "travel": False, "completed_tasks": False}


def reviewed_location(api, requested_id, confirmed_free):
    locations = api.call("GET", b.GROWTH + "/buddy/travel/config").get("locations")
    if not isinstance(locations, list):
        raise b.Stop("TRAVEL_CONFIG_CHANGED")
    if requested_id is None:
        return [{k: loc[k] for k in ("id", "name", "cost", "credit_cost", "energy_cost", "price") if k in loc}
                for loc in locations if isinstance(loc, dict)]
    matches = [loc for loc in locations if isinstance(loc, dict) and str(loc.get("id")) == requested_id]
    if len(matches) != 1:
        raise b.Stop("TRAVEL_LOCATION_REVIEW_REQUIRED")
    loc = matches[0]
    b.identifier(loc.get("id"))
    if any(loc.get(key) not in (None, 0) for key in ("cost", "credit_cost", "energy_cost", "price")):
        raise b.Stop("PAID_TRAVEL_BLOCKED")
    if not confirmed_free:
        raise b.Stop("CONFIRM_FREE_TRAVEL_IN_CLIENT_FIRST")
    return {"travel": True, "travel_location_id": loc["id"], "free_travel_reviewed": True,
            "travel_location_fingerprint": hashlib.sha256(json.dumps(loc, sort_keys=True, ensure_ascii=False).encode()).hexdigest()}


def main(argv=None):
    parser = argparse.ArgumentParser(description="本机授权设置；无参数只预览，不读取登录凭证、不领取积分")
    parser.add_argument("--authorize-daily", action="store_true", help="首次明确授权使用本机登录状态领取每日积分")
    parser.add_argument("--review-travel", action="store_true", help="只读查询旅行地点，不能据缺少费用字段推断免费")
    parser.add_argument("--enable-travel", metavar="ID", help="授权每天派往明确核实的免费地点")
    parser.add_argument("--confirm-free", action="store_true", help="本人已在客户端核实此地点不消耗积分或能量")
    parser.add_argument("--enable-completed-tasks", action="store_true", help="授权领取已达标且未锁定的任务奖励")
    args = parser.parse_args(argv)
    mutations = args.authorize_daily or args.enable_travel is not None or args.enable_completed_tasks
    if args.authorize_daily and (args.enable_travel or args.enable_completed_tasks):
        parser.error("先完成每日积分授权和只读核验，再单独授权可选能力")
    if args.confirm_free and args.enable_travel is None:
        parser.error("--confirm-free 只能与 --enable-travel 一起使用")
    if not mutations and not args.review_travel:
        print(json.dumps({"mode": "preview", "config": daily_config(), "data_directory": str(b.DATA)}, ensure_ascii=False))
        return 0
    if sys.platform != "darwin":
        raise b.Stop("MACOS_REQUIRED")
    os.umask(0o077)
    b.DATA.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = b.DATA / "background-authorization.json"
    with (b.DATA / "run.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise b.Stop("ANOTHER_RUN_IN_PROGRESS") from None
        if args.authorize_daily:
            if path.exists():
                raise b.Stop("EXISTING_AUTHORIZATION_PRESERVED")
            b.atomic(path, daily_config())
            print("每日积分本机授权已保存；尚未安装定时任务，也未领取。")
            return 0
        config = b.authorization(path)
        if args.review_travel or args.enable_travel:
            headers, account = b.session(b.AUTH)
            b.Store(b.DATA / "background-state.json", account)  # Preserve account isolation.
            review = reviewed_location(b.API(headers), args.enable_travel, args.confirm_free)
            if args.enable_travel:
                config.update(review)
            else:
                print(json.dumps({"mode": "read_only", "locations": review}, ensure_ascii=False))
        if args.enable_completed_tasks:
            config["completed_tasks"] = True
        if mutations:
            stamp = datetime.datetime.now(b.ZONE).strftime("%Y%m%d-%H%M%S-%f")
            backup = b.DATA / "backups" / ("config-" + stamp)
            backup.mkdir(mode=0o700, parents=True)
            shutil.copy2(path, backup / path.name)
            b.atomic(path, config)
            print("所选范围已保存；不会接受新任务或消费资源。运行中的定时任务会在后续轮次使用新配置。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except b.Stop as error:
        print(json.dumps({"error": error.code}, ensure_ascii=False)); sys.exit(1)
    except Exception:
        print('{"error":"LOCAL_CONFIGURATION_FAILED"}'); sys.exit(1)
