#!/usr/bin/env python3
"""Local, allowlisted WorkBuddy rewards client. No third-party runtime dependencies.

The desktop client's session is read only when an explicit local authorization
file exists. Tokens never enter state, output, subprocess arguments or logs.
"""
import argparse
import ctypes
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

BASE = "https://copilot.tencent.com"
GROWTH = "/v2/activity/growth"
STATUS = "/v2/billing/meter/checkin-activity-status"
CLAIM = "/v2/billing/meter/daily-checkin"
ZONE = dt.timezone(dt.timedelta(hours=8))
ID_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._-]{0,159}"
DATA = Path.home() / "Library/Application Support/WorkBuddyAutoClaim"
AUTH = Path.home() / "Library/Application Support/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info"
READS = {("POST", STATUS), ("GET", GROWTH + "/tasks"),
         ("GET", GROWTH + "/buddy/travel/status"),
         ("GET", GROWTH + "/buddy/travel/config")}
WRITES = {("POST", CLAIM), ("POST", GROWTH + "/buddy/travel/claim"),
          ("POST", GROWTH + "/buddy/travel/depart")}
SESSION_REASONS = frozenset({"CHILD_TIMEOUT", "CHILD_TERMINATED", "CHILD_START_FAILED",
                             "INVALID_PRIVATE_OUTPUT", "HELPER_REJECTED", "DECRYPT_CHECK_FAILED",
                             "KEY_PROVIDER_UNAVAILABLE", "KEY_PAYLOAD_REJECTED", "KEY_MISMATCH",
                             "AUTHENTICATION_FAILED", "TOKEN_REJECTED"})


class Stop(Exception):
    def __init__(self, code, not_sent=False, reason=None):
        self.code = code
        self.not_sent = not_sent
        self.reason = reason if reason in SESSION_REASONS else None
        super().__init__(code)


def now():
    return dt.datetime.now(ZONE)


def due(moment):
    return moment.hour * 60 + moment.minute >= 390


def screen_state():
    """Read only the lock bit, for test evidence; never controls the desktop."""
    try:
        cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        cg.CGSessionCopyCurrentDictionary.restype = ctypes.c_void_p
        cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
        cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        cf.CFDictionaryGetValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        cf.CFDictionaryGetValue.restype = ctypes.c_void_p
        cf.CFBooleanGetValue.argtypes = [ctypes.c_void_p]
        cf.CFBooleanGetValue.restype = ctypes.c_bool
        cf.CFGetTypeID.argtypes = [ctypes.c_void_p]
        cf.CFGetTypeID.restype = ctypes.c_ulong
        cf.CFBooleanGetTypeID.restype = ctypes.c_ulong
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        current = cg.CGSessionCopyCurrentDictionary()
        if not current:
            return "UNKNOWN"
        key = cf.CFStringCreateWithCString(None, b"CGSSessionScreenIsLocked", 0x08000100)
        try:
            value = cf.CFDictionaryGetValue(current, key)
            if not value:
                return "UNLOCKED"
            if cf.CFGetTypeID(value) != cf.CFBooleanGetTypeID():
                return "UNKNOWN"
            return "LOCKED" if cf.CFBooleanGetValue(value) else "UNLOCKED"
        finally:
            cf.CFRelease(key)
            cf.CFRelease(current)
    except Exception:
        return "UNKNOWN"


def atomic(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".writing-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def load_json(path, missing=None):
    try:
        with path.open(encoding="utf-8") as stream:
            value = json.load(stream)
        if not isinstance(value, dict):
            raise Stop("INVALID_LOCAL_FORMAT")
        return value
    except FileNotFoundError:
        if missing is not None:
            return missing
        raise Stop("LOCAL_FILE_MISSING") from None
    except (OSError, ValueError):
        raise Stop("LOCAL_FILE_UNREADABLE") from None


def unwrap(value):
    # Accept only known envelope objects; never search arbitrary nested values.
    for _ in range(5):
        if not isinstance(value, dict):
            raise Stop("RESPONSE_FORMAT_CHANGED")
        if value.get("code") not in (None, 0, 200, "0", "200"):
            raise Stop("BUSINESS_REJECTED")
        if value.get("success") is False or value.get("error"):
            raise Stop("BUSINESS_REJECTED")
        envelopes = [value[k] for k in ("data", "result", "resp", "response")
                     if isinstance(value.get(k), dict)]
        if not envelopes:
            return value
        if len(envelopes) != 1:
            raise Stop("RESPONSE_FORMAT_CHANGED")
        value = envelopes[0]
    raise Stop("RESPONSE_FORMAT_CHANGED")


def safe_number(value):
    return value if type(value) in (int, float) and 0 <= value <= 10000000 else None


def identifier(value):
    if type(value) is int and value >= 0:
        return str(value)
    if isinstance(value, str) and re.fullmatch(ID_PATTERN, value):
        return value
    raise Stop("UNSAFE_IDENTIFIER")


def authorization(path):
    config = load_json(path)
    if config.get("use_desktop_session") is not True or config.get("endpoint") != BASE:
        raise Stop("AUTHORIZATION_REQUIRED")
    if config.get("daily") is not True:
        raise Stop("AUTHORIZATION_REQUIRED")
    return config


def session(path):
    data = load_json(path)
    auth, account = data.get("auth"), data.get("account")
    if not isinstance(auth, dict) or not isinstance(account, dict):
        raise Stop("LOGIN_REQUIRED")
    endpoint = auth.get("endpoint") or BASE
    if not isinstance(endpoint, str) or endpoint.rstrip("/") != BASE:
        raise Stop("ENDPOINT_NOT_ALLOWED")
    token = auth.get("accessToken")
    if isinstance(token, dict):
        token = desktop_token(token)
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9._~+/-]{1,32768}=*", token):
        raise Stop("LOGIN_REQUIRED")
    uid = account.get("uid")
    headers = {"Accept": "application/json", "Content-Type": "application/json",
               "User-Agent": "WorkBuddy", "Authorization": "Bearer " + token}
    for key, value in (("X-User-Id", uid), ("X-Enterprise-Id", account.get("enterpriseId")),
                       ("X-Tenant-Id", account.get("enterpriseId")), ("X-Domain", auth.get("domain"))):
        if value in (None, "") and key != "X-User-Id":
            continue
        if not isinstance(value, str) or not re.fullmatch(r"[\x21-\x7e]{1,2048}", value):
            raise Stop("LOGIN_FORMAT_CHANGED")
        headers[key] = value
    account_key = hashlib.sha256((uid + "|" + str(account.get("enterpriseId", "")) + "|" + str(auth.get("domain", ""))).encode()).hexdigest()[:24]
    return headers, account_key


def desktop_token(wrapper):
    """Decrypt only the authorized access token, through private process pipes.

    Use the installed WorkBuddy runtime's own native key provider. Never persist
    keys/tokens or put them in argv, environment values, console output or logs.
    Unknown schemes stop; we never disable the desktop app's encryption.
    """
    if (set(wrapper) != {"$wbEncrypted", "envelope"} or
            type(wrapper.get("$wbEncrypted")) is not int or wrapper["$wbEncrypted"] != 1 or
            not isinstance(wrapper.get("envelope"), str) or len(wrapper["envelope"]) > 65536):
        raise Stop("ENCRYPTED_LOGIN_UNSUPPORTED")
    executable = Path("/Applications/WorkBuddy.app/Contents/MacOS/Electron")
    helper = Path(__file__).resolve().with_name("desktop_session.cjs")
    if not executable.is_file() or not helper.is_file():
        raise Stop("LOGIN_HELPER_UNAVAILABLE")
    # Do not inherit NODE_OPTIONS or injection/search-path overrides.
    env = {key: os.environ[key] for key in ("HOME", "TMPDIR", "LANG") if key in os.environ}
    env["ELECTRON_RUN_AS_NODE"] = "1"
    # Retry a local process failure once, before constructing any network client.
    # Authentication, key/schema validation and malformed output never retry.
    for attempt in range(2):
        try:
            result = subprocess.run([str(executable), str(helper), "--private-pipe"],
                                    input=json.dumps(wrapper).encode(), stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, env=env, timeout=15, check=False)
        except subprocess.TimeoutExpired:
            reason = "CHILD_TIMEOUT"
        except OSError:
            raise Stop("LOGIN_DECRYPT_FAILED", reason="CHILD_START_FAILED") from None
        else:
            if result.returncode < 0:
                reason = "CHILD_TERMINATED"
            elif result.returncode:
                # Never log raw stderr; recognize only exact, fixed helper markers.
                marker = (result.stderr or b"").strip()
                known = {("WB_SESSION_ERROR=" + value).encode(): value
                         for value in SESSION_REASONS}
                raise Stop("LOGIN_DECRYPT_FAILED", reason=known.get(marker, "HELPER_REJECTED"))
            else:
                try:
                    token = result.stdout.decode("ascii")
                except (UnicodeError, AttributeError):
                    raise Stop("LOGIN_DECRYPT_FAILED", reason="INVALID_PRIVATE_OUTPUT") from None
                if not re.fullmatch(r"[A-Za-z0-9._~+/-]{1,32768}=*", token):
                    raise Stop("LOGIN_DECRYPT_FAILED", reason="INVALID_PRIVATE_OUTPUT")
                return token
        if attempt == 0:
            time.sleep(0.5)
        else:
            raise Stop("LOGIN_DECRYPT_FAILED", reason=reason)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Stop("REDIRECT_BLOCKED")


class API:
    def __init__(self, headers, writes=False):
        self.headers = headers
        self.writes = writes
        self.deadline = time.monotonic() + 150
        self.opener = urllib.request.build_opener(
            NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context()))

    def call(self, method, path, payload=None):
        is_write = (method, path) not in READS
        task_claim = method == "POST" and re.fullmatch(
            re.escape(GROWTH) + r"/tasks/" + ID_PATTERN + r"/claim", path)
        if is_write and (not self.writes or ((method, path) not in WRITES and not task_claim)):
            raise Stop("ACTION_NOT_ALLOWED", not_sent=True)
        if self.deadline - time.monotonic() < 2:
            raise Stop("TIME_BUDGET_EXHAUSTED", not_sent=True)
        body = json.dumps(payload if payload is not None else {}).encode() if method == "POST" else None
        request = urllib.request.Request(BASE + path, data=body, headers=self.headers, method=method)
        try:
            with self.opener.open(request, timeout=min(20, self.deadline - time.monotonic())) as response:
                if response.status != 200:
                    raise Stop("HTTP_REJECTED")
                raw = response.read(2 * 1024 * 1024 + 1)
                if len(raw) > 2 * 1024 * 1024:
                    raise Stop("RESPONSE_TOO_LARGE")
                return unwrap(json.loads(raw))
        except urllib.error.HTTPError as error:
            raise Stop("LOGIN_REJECTED" if error.code in (401, 403) else "HTTP_" + str(error.code),
                       not_sent=error.code in (401, 403)) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise Stop("NETWORK_UNAVAILABLE") from None
        except (ValueError, TypeError):
            raise Stop("RESPONSE_FORMAT_CHANGED") from None


class Store:
    def __init__(self, path, account_key):
        self.path = path
        self.data = load_json(path, {})
        if self.data and self.data.get("account") != account_key:
            raise Stop("ACCOUNT_CHANGED_REVIEW_REQUIRED")
        self.data.setdefault("account", account_key)
        self.data.setdefault("actions", {})
        if not isinstance(self.data["actions"], dict):
            raise Stop("INVALID_LOCAL_FORMAT")

    def save(self):
        atomic(self.path, self.data)

    def mark(self, key, status, **fields):
        item = self.data["actions"].setdefault(key, {})
        item.update(status=status, time=now().isoformat(), **fields)
        self.save()

    def pending(self, key):
        return key in self.data["actions"] and self.data["actions"][key]["status"] != "verified"


def daily_status(value):
    checked, active = value.get("today_checked_in"), value.get("active")
    if type(checked) is not bool or type(active) is not bool:
        raise Stop("DAILY_SCHEMA_CHANGED")
    return checked, active


def task_list(value):
    tasks = value.get("tasks")
    if not isinstance(tasks, list) or any(not isinstance(t, dict) for t in tasks):
        raise Stop("TASK_SCHEMA_CHANGED")
    return tasks


class Rewards:
    def __init__(self, api, store, config, moment=None):
        self.api, self.store, self.config = api, store, config
        self.day = (moment or now()).date().isoformat()
        self.results = []

    def record(self, status, **numbers):
        self.results.append(dict(status=status, **numbers))

    def action(self, key, path, payload):
        if now().date().isoformat() != self.day:
            raise Stop("DAY_CHANGED_NO_WRITE")
        if key in self.store.data["actions"] and self.store.data["actions"][key]["status"] != "retryable":
            raise Stop("PREVIOUS_REQUEST_UNVERIFIED")
        # A crash or timeout after this point must never cause blind retransmission.
        self.store.mark(key, "pending", target_id=payload.get("location_id", payload.get("record_id")))
        try:
            return self.api.call("POST", path, payload)
        except Stop as error:
            if error.not_sent:
                self.store.mark(key, "retryable")
            raise

    def daily(self):
        status = self.api.call("POST", STATUS)
        checked, active = daily_status(status)
        key = "daily:" + self.day
        if checked:
            self.store.mark(key, "verified")
            self.store.data["daily_confirmed"] = self.day
            self.store.save()
            self.record("DAILY_ALREADY_VERIFIED", today_credit=safe_number(status.get("today_credit")))
        elif not active:
            self.record("DAILY_INACTIVE")
        else:
            problem = None
            try:
                self.action(key, CLAIM, {})
            except Stop as error:
                problem = error
            if problem and problem.code in ("LOGIN_REJECTED", "PREVIOUS_REQUEST_UNVERIFIED"):
                raise problem
            # Always require a second authoritative status read, even after HTTP 200.
            fresh = self.api.call("POST", STATUS)
            if daily_status(fresh)[0]:
                self.store.mark(key, "verified")
                self.store.data["daily_confirmed"] = self.day
                self.store.save()
                self.record("DAILY_CLAIM_VERIFIED", today_credit=safe_number(fresh.get("today_credit")))
            else:
                raise problem or Stop("DAILY_CLAIM_UNVERIFIED")

    def travel(self):
        status = self.api.call("GET", GROWTH + "/buddy/travel/status")
        state = status.get("state")
        if state not in ("idle", "traveling", "arrived"):
            raise Stop("TRAVEL_SCHEMA_CHANGED")
        depart_key = "depart:" + self.day
        if state == "traveling":
            previous = self.store.data["actions"].get(depart_key, {})
            loc = status.get("location") or {}
            if self.store.pending(depart_key) and str(loc.get("id")) == str(previous.get("target_id")) and previous.get("target_id") is not None:
                self.store.mark(depart_key, "verified")
            self.record("BUDDY_TRAVELING")
            return
        if state == "arrived":
            rid = identifier(status.get("record_id"))
            claim_key = "gift:" + rid
            result = self.action(claim_key, GROWTH + "/buddy/travel/claim", {"record_id": status["record_id"]})
            credit = safe_number(result.get("reward_credit"))
            if type(credit) is not int or credit <= 0:
                raise Stop("GIFT_CLAIM_UNVERIFIED")
            self.store.mark(claim_key, "receipt", credit=credit)
            status = self.api.call("GET", GROWTH + "/buddy/travel/status")
            state = status.get("state")
            if state not in ("idle", "traveling"):
                raise Stop("GIFT_CLAIM_UNVERIFIED")
            self.store.mark(claim_key, "verified")
            self.record("GIFT_CLAIM_VERIFIED", credit=credit)
        if state != "idle":
            return
        # A saved positive receipt plus an idle status reconciles a failed follow-up read.
        for key, value in list(self.store.data["actions"].items()):
            if key.startswith("gift:") and value["status"] == "receipt" and type(value.get("credit")) is int and value["credit"] > 0:
                self.store.mark(key, "verified")
                self.record("GIFT_CLAIM_VERIFIED", credit=value["credit"])
        if any(key.startswith("gift:") and value["status"] != "verified"
               for key, value in self.store.data["actions"].items()):
            raise Stop("GIFT_PREVIOUS_RESULT_UNKNOWN")
        limit = status.get("daily_limit_reached")
        if type(limit) is not bool:
            raise Stop("TRAVEL_LIMIT_UNKNOWN")
        if limit:
            self.record("TRAVEL_DAILY_LIMIT")
            return
        previous = self.store.data["actions"].get(depart_key)
        if previous and previous["status"] != "retryable":
            self.record("TRAVEL_ALREADY_REQUESTED_TODAY")
            return
        config = self.api.call("GET", GROWTH + "/buddy/travel/config")
        locations = config.get("locations")
        if not isinstance(locations, list):
            raise Stop("TRAVEL_CONFIG_CHANGED")
        # Use a reviewed destination only. Do not choose an arbitrary first location.
        chosen = [loc for loc in locations if isinstance(loc, dict)
                  and str(loc.get("id")) == str(self.config.get("travel_location_id", ""))]
        if len(chosen) != 1:
            raise Stop("TRAVEL_LOCATION_REVIEW_REQUIRED")
        location = chosen[0]
        identifier(location.get("id"))
        fingerprint = hashlib.sha256(json.dumps(location, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if self.config.get("free_travel_reviewed") is not True or self.config.get("travel_location_fingerprint") != fingerprint:
            raise Stop("TRAVEL_CONFIG_REVIEW_REQUIRED")
        for field in ("cost", "credit_cost", "energy_cost", "price"):
            if field in location and location[field] not in (0, None):
                raise Stop("PAID_TRAVEL_BLOCKED")
        self.action(depart_key, GROWTH + "/buddy/travel/depart", {"location_id": location["id"]})
        fresh = self.api.call("GET", GROWTH + "/buddy/travel/status")
        fresh_location = fresh.get("location")
        if fresh.get("state") != "traveling" or not isinstance(fresh_location, dict) or str(fresh_location.get("id")) != str(location["id"]):
            raise Stop("TRAVEL_DEPART_UNVERIFIED")
        self.store.mark(depart_key, "verified")
        self.record("TRAVEL_DEPART_VERIFIED")

    def tasks(self):
        tasks = task_list(self.api.call("GET", GROWTH + "/tasks"))
        claimed = [t for t in tasks if t.get("accept_status") == "claimed"]
        for task in claimed:
            key = "task:" + identifier(task.get("task_code"))
            if self.store.pending(key):
                self.store.mark(key, "verified")
        completed = [t for t in tasks if t.get("accept_status") == "completed" and t.get("locked") is False]
        if not completed:
            self.record("NO_COMPLETED_TASKS")
        for task in completed[:8]:
            code = identifier(task.get("task_code"))
            key = "task:" + code
            try:
                self.action(key, GROWTH + "/tasks/" + code + "/claim", {})
                fresh = task_list(self.api.call("GET", GROWTH + "/tasks"))
                matched = [t for t in fresh if t.get("task_code") == code]
                if len(matched) != 1 or matched[0].get("accept_status") != "claimed":
                    raise Stop("TASK_CLAIM_UNVERIFIED")
                self.store.mark(key, "verified")
                self.record("TASK_CLAIM_VERIFIED")
            except Stop as error:
                if error.code == "LOGIN_REJECTED":
                    raise
                self.record(error.code)

    def run(self, daily=True, growth=True):
        for name, enabled in (("daily", daily), ("travel", growth and self.config.get("travel") is True),
                              ("tasks", growth and self.config.get("completed_tasks") is True)):
            if not enabled:
                continue
            try:
                getattr(self, name)()
            except Stop as error:
                self.record(error.code)
                if error.code in ("LOGIN_REJECTED", "TIME_BUDGET_EXHAUSTED", "NETWORK_UNAVAILABLE"):
                    break
        return self.results


GOOD = {"DAILY_ALREADY_VERIFIED", "DAILY_CLAIM_VERIFIED", "DAILY_INACTIVE", "BUDDY_TRAVELING",
        "GIFT_CLAIM_VERIFIED", "TRAVEL_DAILY_LIMIT", "TRAVEL_ALREADY_REQUESTED_TODAY",
        "TRAVEL_DEPART_VERIFIED", "NO_COMPLETED_TASKS", "TASK_CLAIM_VERIFIED"}
MESSAGES = {
    "DAILY_ALREADY_VERIFIED": "服务端确认今日积分已领取",
    "DAILY_CLAIM_VERIFIED": "领取后服务端确认今日积分已领取",
    "DAILY_INACTIVE": "当前加油站活动未开放",
    "BUDDY_TRAVELING": "宠物旅行中",
    "GIFT_CLAIM_VERIFIED": "已领取旅行礼物",
    "TRAVEL_DAILY_LIMIT": "今天的旅行名额已用完",
    "TRAVEL_ALREADY_REQUESTED_TODAY": "今天已发出旅行请求，不重复派出",
    "TRAVEL_DEPART_VERIFIED": "宠物已出发旅行",
    "NO_COMPLETED_TASKS": "没有已达标待领取的任务",
    "TASK_CLAIM_VERIFIED": "任务奖励已领取并核实",
    "LOGIN_REJECTED": "登录已失效，请在 WorkBuddy 重新登录",
    "LOGIN_REQUIRED": "没有可用登录状态，请打开 WorkBuddy 检查登录",
    "ENCRYPTED_LOGIN_UNSUPPORTED": "登录凭证格式已变化，自动领取需要兼容修复",
    "LOGIN_DECRYPT_FAILED": "读取本机登录状态失败，自动领取需要检查",
    "LOGIN_HELPER_UNAVAILABLE": "登录兼容组件不可用，自动领取需要检查",
    "NETWORK_UNAVAILABLE": "网络暂不可用，稍后补查",
    "PREVIOUS_REQUEST_UNVERIFIED": "上次请求结果未确认，已停止重复提交",
}


def diagnostics(api, config):
    # Only allowlisted state fields leave this process; no full API response dumps.
    value = api.call("POST", STATUS)
    checked, active = daily_status(value)
    result = {"daily": {"checked": checked, "active": active,
                        "today_credit": safe_number(value.get("today_credit"))}}
    if config.get("travel"):
        travel = api.call("GET", GROWTH + "/buddy/travel/status")
        result["travel"] = {k: travel.get(k) for k in ("state", "daily_limit_reached", "arrive_at", "server_now")}
        locations = api.call("GET", GROWTH + "/buddy/travel/config").get("locations", [])
        result["locations"] = [dict({k: loc[k] for k in ("id", "name", "duration_hours", "cost", "credit_cost", "energy_cost", "price") if k in loc},
                                    fields=sorted(loc), fingerprint=hashlib.sha256(json.dumps(loc, sort_keys=True, ensure_ascii=False).encode()).hexdigest())
                               for loc in locations if isinstance(loc, dict)]
    if config.get("completed_tasks"):
        tasks = task_list(api.call("GET", GROWTH + "/tasks"))
        result["tasks"] = [{k: task.get(k) for k in ("task_code", "accept_status", "locked", "reward_credit", "reward_energy")} for task in tasks]
    return result


def notify_login(rows, mode):
    """Best effort local reminder. Notification failures never stop reward processing."""
    if mode not in ("scheduled", "run"):
        return "SKIPPED"
    statuses = {row.get("status") for row in rows}
    failures = statuses - GOOD
    # Brief network interruptions remain quiet; persistent outages do not.
    if not failures:
        return "NOT_NEEDED"
    if failures == {"NETWORK_UNAVAILABLE"}:
        try:
            health = load_json(DATA / "background-health.json", {})
            if time.time() - health.get("network_failed_since", time.time()) < 3600:
                return "NOT_NEEDED"
        except Exception:
            pass  # Broken bookkeeping should escalate, not silence the error.
    login = bool(failures.intersection({"LOGIN_REJECTED", "LOGIN_REQUIRED"}))
    helper = DATA / "WorkBuddy领取提醒.app"
    if not helper.is_dir():
        return "HELPER_MISSING"
    path = DATA / "notification-state.json"
    try:
        state = load_json(path, {})
        day = now().date().isoformat()
        if state.get("queued_day") == day:
            return "ALREADY_NOTIFIED_TODAY"
        epoch = time.time()
        if epoch - state.get("last_attempt", 0) < 3600:
            return "RETRY_LATER"
        request_id = os.urandom(12).hex()
        state["last_attempt"] = epoch
        atomic(path, state)
        subprocess.run(["/usr/bin/open", "-g", "-W", "-n", "-a", str(helper),
                        "--args", "--login" if login else "--attention", request_id], timeout=12,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        receipt = load_json(DATA / "notification-result.json", {})
        if receipt.get("request_id") != request_id:
            return "NO_RECEIPT"
        status = receipt.get("status")
        if status in ("QUEUED", "WORKBUDDY_OPENED"):
            state["queued_day"] = day
            atomic(path, state)
            return "QUEUED"
        return "NOT_AUTHORIZED" if status == "NOT_AUTHORIZED" else "SEND_FAILED"
    except Exception:
        return "SEND_FAILED"


def log_result(rows, mode):
    item = {"time": now().isoformat(), "mode": mode, "screen_state": screen_state(), "results": rows,
            "needs_attention": any(row["status"] not in GOOD for row in rows)}
    item["report"] = "；".join(MESSAGES.get(row["status"], "需要检查：" + row["status"]) for row in rows)
    if mode in ("run", "scheduled"):
        try:
            health = load_json(DATA / "background-health.json", {})
            if any(row["status"] == "NETWORK_UNAVAILABLE" for row in rows):
                health.setdefault("network_failed_since", time.time())
            else:
                health.pop("network_failed_since", None)
            atomic(DATA / "background-health.json", health)
        except Exception:
            pass  # Health bookkeeping must never hide the original result.
    item["notification"] = notify_login(rows, mode)
    target = DATA / "background-events.jsonl"
    if target.exists() and target.stat().st_size > 2 * 1024 * 1024:
        os.replace(target, DATA / "background-events.previous.jsonl")
    fd = os.open(target, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(item, ensure_ascii=False) + "\n")
    atomic(DATA / "background-latest.json", item)
    print(json.dumps(item, ensure_ascii=False))
    return 1 if item["needs_attention"] else 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("diagnose", "run", "scheduled"))
    args = parser.parse_args()
    if args.mode != 'diagnose' and load_json(DATA / 'execution-owner.json', {}).get('source') == 'cloud':
        print(json.dumps({'status': 'CLOUD_OWNS_SCHEDULE', 'hint': 'Use cloud/cloud_control.py status; keep the local claimant stopped.'}))
        return 1
    os.umask(0o077)
    DATA.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (DATA / "run.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        if args.mode != 'diagnose' and load_json(DATA / 'execution-owner.json', {}).get('source') == 'cloud':
            print(json.dumps({'status': 'CLOUD_OWNS_SCHEDULE'}))
            return 1
        moment = now()
        if args.mode == "scheduled" and (not due(moment) or not (DATA / "enabled").is_file()):
            return 0
        try:
            config = authorization(DATA / "background-authorization.json")
            headers, account_key = session(AUTH)
            api = API(headers, writes=args.mode != "diagnose")
            if args.mode == "diagnose":
                before = screen_state()
                checks = diagnostics(api, config)
                print(json.dumps({"screen_before": before, "checks": checks, "screen_after": screen_state()}, ensure_ascii=False))
                return 0
            store = Store(DATA / "background-state.json", account_key)
            epoch = time.time()
            day = moment.date().isoformat()
            daily = store.data.get("daily_confirmed") != day
            growth = epoch >= store.data.get("next_growth", 0)
            if args.mode == "scheduled":
                if epoch < store.data.get("retry_after", 0) or (not daily and not growth):
                    return 0
            else:
                daily = growth = True
            rows = Rewards(api, store, config, moment).run(daily, growth)
            if growth:
                store.data["next_growth"] = epoch + 1800
            errors = [r for r in rows if r["status"] not in GOOD]
            delay = 3600 if any(r["status"] == "LOGIN_REJECTED" for r in errors) else 300
            store.data["retry_after"] = epoch + delay if errors else 0
            store.save()
            return log_result(rows, args.mode)
        except Stop as error:
            row = {"status": error.code}
            if error.reason:
                row["reason"] = error.reason
            return log_result([row], args.mode)
        except Exception:
            # Never print exception text: it can contain request headers or session fields.
            return log_result([{"status": "LOCAL_UNEXPECTED_ERROR"}], args.mode)


if __name__ == "__main__":
    signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(Stop("TIME_BUDGET_EXHAUSTED")))
    signal.alarm(175)
    sys.exit(main())
