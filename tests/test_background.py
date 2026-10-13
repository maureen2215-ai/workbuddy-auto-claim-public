import importlib.util
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

spec = importlib.util.spec_from_file_location("background", Path(__file__).resolve().parents[1] / "src/background.py")
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)


class FakeAPI:
    def __init__(self, calls):
        self.calls = list(calls)
        self.sent = []

    def call(self, method, path, payload=None):
        self.sent.append((method, path, payload))
        if not self.calls:
            raise AssertionError("unexpected request")
        expected_method, expected_path, value = self.calls.pop(0)
        assert (method, path) == (expected_method, expected_path)
        if isinstance(value, Exception):
            raise value
        return value


class RewardsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.json"
        self.store = b.Store(self.path, "test-account")
        self.config = {"daily": True, "travel": True, "completed_tasks": True, "travel_location_id": "cafe"}
        self.day = b.now().date().isoformat()

    def tearDown(self):
        self.temp.cleanup()

    def rewards(self, calls):
        self.api = FakeAPI(calls)
        return b.Rewards(self.api, self.store, self.config)

    def test_already_claimed_is_read_only(self):
        r = self.rewards([("POST", b.STATUS, {"active": True, "today_checked_in": True})])
        r.daily()
        self.assertEqual(len(self.api.sent), 1)
        self.assertEqual(self.store.data["daily_confirmed"], self.day)

    def test_missing_daily_fields_never_claim(self):
        for value in ({}, {"active": True}, {"active": True, "today_checked_in": "false"}):
            r = self.rewards([("POST", b.STATUS, value)])
            with self.assertRaises(b.Stop):
                r.daily()
            self.assertEqual(len(self.api.sent), 1)

    def test_daily_timeout_verified_by_second_read(self):
        r = self.rewards([("POST", b.STATUS, {"active": True, "today_checked_in": False}),
                          ("POST", b.CLAIM, b.Stop("NETWORK_UNAVAILABLE")),
                          ("POST", b.STATUS, {"active": True, "today_checked_in": True})])
        r.daily()
        self.assertEqual(r.results[0]["status"], "DAILY_CLAIM_VERIFIED")
        self.assertFalse(self.store.pending("daily:" + self.day))

    def test_unknown_daily_not_repeated_after_restart(self):
        r = self.rewards([("POST", b.STATUS, {"active": True, "today_checked_in": False}),
                          ("POST", b.CLAIM, {}),
                          ("POST", b.STATUS, {"active": True, "today_checked_in": False})])
        with self.assertRaises(b.Stop):
            r.daily()
        self.store = b.Store(self.path, "test-account")
        r = self.rewards([("POST", b.STATUS, {"active": True, "today_checked_in": False})])
        with self.assertRaises(b.Stop) as error:
            r.daily()
        self.assertEqual(error.exception.code, "PREVIOUS_REQUEST_UNVERIFIED")
        self.assertEqual(len(self.api.sent), 1)

    def test_request_intent_durable_before_network(self):
        r = self.rewards([])
        key = "daily:" + self.day
        def call(*args):
            self.assertEqual(json.loads(self.path.read_text())["actions"][key]["status"], "pending")
            raise b.Stop("NETWORK_UNAVAILABLE")
        self.api.call = call
        with self.assertRaises(b.Stop):
            r.action(key, b.CLAIM, {})

    def test_corrupt_state_and_other_account_stop(self):
        self.path.write_text("invalid")
        with self.assertRaises(b.Stop):
            b.Store(self.path, "test-account")
        self.path.write_text('{"account":"another-account"}')
        with self.assertRaises(b.Stop) as error:
            b.Store(self.path, "test-account")
        self.assertEqual(error.exception.code, "ACCOUNT_CHANGED_REVIEW_REQUIRED")

    def test_no_daily_claim_when_activity_inactive(self):
        r = self.rewards([("POST", b.STATUS, {"active": False, "today_checked_in": False})])
        r.daily()
        self.assertEqual(r.results, [{"status": "DAILY_INACTIVE"}])

    def test_traveling_or_daily_limit_never_depart(self):
        for response in ({"state": "traveling"}, {"state": "idle", "daily_limit_reached": True}):
            r = self.rewards([("GET", b.GROWTH + "/buddy/travel/status", response)])
            r.travel()
            self.assertEqual(len(self.api.sent), 1)

    def test_missing_travel_limit_never_depart(self):
        r = self.rewards([("GET", b.GROWTH + "/buddy/travel/status", {"state": "idle"})])
        with self.assertRaises(b.Stop):
            r.travel()

    def test_gift_receipt_required_before_departure(self):
        r = self.rewards([("GET", b.GROWTH + "/buddy/travel/status", {"state": "arrived", "record_id": "r1"}),
                          ("POST", b.GROWTH + "/buddy/travel/claim", {})])
        with self.assertRaises(b.Stop):
            r.travel()
        self.assertTrue(self.store.pending("gift:r1"))
        r = self.rewards([("GET", b.GROWTH + "/buddy/travel/status", {"state": "idle", "daily_limit_reached": False})])
        with self.assertRaises(b.Stop) as error:
            r.travel()
        self.assertEqual(error.exception.code, "GIFT_PREVIOUS_RESULT_UNKNOWN")

    def test_valid_gift_then_daily_limit(self):
        r = self.rewards([("GET", b.GROWTH + "/buddy/travel/status", {"state": "arrived", "record_id": "r1"}),
                          ("POST", b.GROWTH + "/buddy/travel/claim", {"reward_credit": 30}),
                          ("GET", b.GROWTH + "/buddy/travel/status", {"state": "idle", "daily_limit_reached": True})])
        r.travel()
        self.assertEqual(r.results[0], {"status": "GIFT_CLAIM_VERIFIED", "credit": 30})

    def test_paid_or_unknown_destination_blocked(self):
        for loc in ({"id": "unknown"}, {"id": "cafe", "credit_cost": 10}):
            self.config.update(free_travel_reviewed=True, travel_location_fingerprint=hashlib.sha256(json.dumps(loc, sort_keys=True, ensure_ascii=False).encode()).hexdigest())
            r = self.rewards([("GET", b.GROWTH + "/buddy/travel/status", {"state": "idle", "daily_limit_reached": False}),
                              ("GET", b.GROWTH + "/buddy/travel/config", {"locations": [loc]})])
            with self.assertRaises(b.Stop) as error:
                r.travel()
            self.assertEqual(error.exception.code, "PAID_TRAVEL_BLOCKED" if loc["id"] == "cafe" else "TRAVEL_LOCATION_REVIEW_REQUIRED")
            self.assertEqual(len(self.api.sent), 2)

    def test_departure_requires_matching_live_destination(self):
        self.config.update(free_travel_reviewed=True, travel_location_fingerprint=hashlib.sha256(json.dumps({"id": "cafe"}, sort_keys=True, ensure_ascii=False).encode()).hexdigest())
        r = self.rewards([("GET", b.GROWTH + "/buddy/travel/status", {"state": "idle", "daily_limit_reached": False}),
                          ("GET", b.GROWTH + "/buddy/travel/config", {"locations": [{"id": "cafe"}]}),
                          ("POST", b.GROWTH + "/buddy/travel/depart", {}),
                          ("GET", b.GROWTH + "/buddy/travel/status", {"state": "traveling", "location": {"id": "cafe"}})])
        r.travel()
        self.assertEqual(r.results[0]["status"], "TRAVEL_DEPART_VERIFIED")

    def test_tasks_need_completed_and_explicit_unlocked(self):
        tasks = [{"task_code": "a", "accept_status": "completed"},
                 {"task_code": "b", "accept_status": "completed", "locked": True},
                 {"task_code": "c", "accept_status": "not_accepted", "locked": False}]
        r = self.rewards([("GET", b.GROWTH + "/tasks", {"tasks": tasks})])
        r.tasks()
        self.assertEqual(len(self.api.sent), 1)

    def test_task_reward_requires_claimed_state(self):
        completed = {"task_code": "safe_task", "accept_status": "completed", "locked": False}
        r = self.rewards([("GET", b.GROWTH + "/tasks", {"tasks": [completed]}),
                          ("POST", b.GROWTH + "/tasks/safe_task/claim", {}),
                          ("GET", b.GROWTH + "/tasks", {"tasks": [completed]})])
        r.tasks()
        self.assertEqual(r.results[0]["status"], "TASK_CLAIM_UNVERIFIED")
        self.assertTrue(self.store.pending("task:safe_task"))

    def test_midnight_blocks_write(self):
        r = self.rewards([])
        r.day = "2000-01-01"
        with self.assertRaises(b.Stop) as error:
            r.action("daily:2000-01-01", b.CLAIM, {})
        self.assertEqual(error.exception.code, "DAY_CHANGED_NO_WRITE")
        self.assertEqual(self.api.sent, [])

    def test_confirmed_rejection_can_retry(self):
        key = "daily:" + self.day
        r = self.rewards([("POST", b.CLAIM, b.Stop("LOGIN_REJECTED", not_sent=True)),
                          ("POST", b.CLAIM, {})])
        with self.assertRaises(b.Stop):
            r.action(key, b.CLAIM, {})
        self.assertEqual(self.store.data["actions"][key]["status"], "retryable")
        r.action(key, b.CLAIM, {})
        self.assertEqual(len(self.api.sent), 2)

    def test_verified_action_cannot_resubmit(self):
        r = self.rewards([])
        self.store.mark("task:test", "verified")
        with self.assertRaises(b.Stop):
            r.action("task:test", b.GROWTH + "/tasks/test/claim", {})
        self.assertEqual(self.api.sent, [])

    def test_zero_gift_does_not_depart(self):
        r = self.rewards([("GET", b.GROWTH + "/buddy/travel/status", {"state": "arrived", "record_id": "r1"}),
                          ("POST", b.GROWTH + "/buddy/travel/claim", {"reward_credit": 0})])
        with self.assertRaises(b.Stop):
            r.travel()
        self.assertTrue(self.store.pending("gift:r1"))

    def test_saved_gift_receipt_reconciles_before_next_trip(self):
        self.store.mark("gift:r1", "receipt", credit=7)
        r = self.rewards([("GET", b.GROWTH + "/buddy/travel/status", {"state": "idle", "daily_limit_reached": True})])
        r.travel()
        self.assertFalse(self.store.pending("gift:r1"))
        self.assertEqual(r.results[0], {"status": "GIFT_CLAIM_VERIFIED", "credit": 7})

    def test_manual_other_trip_not_attributed_to_program(self):
        self.store.mark("depart:" + self.day, "pending", target_id="cafe")
        r = self.rewards([("GET", b.GROWTH + "/buddy/travel/status", {"state": "traveling", "location": {"id": "beach"}})])
        r.travel()
        self.assertTrue(self.store.pending("depart:" + self.day))
        self.assertEqual(len(self.api.sent), 1)

    def test_changed_travel_config_needs_review(self):
        self.config.update(free_travel_reviewed=True, travel_location_fingerprint="old")
        r = self.rewards([("GET", b.GROWTH + "/buddy/travel/status", {"state": "idle", "daily_limit_reached": False}),
                          ("GET", b.GROWTH + "/buddy/travel/config", {"locations": [{"id": "cafe"}]})])
        with self.assertRaises(b.Stop) as error:
            r.travel()
        self.assertEqual(error.exception.code, "TRAVEL_CONFIG_REVIEW_REQUIRED")

    def test_no_write_without_saved_authorization(self):
        with self.assertRaises(b.Stop):
            b.authorization(Path(self.temp.name) / "absent")


class TransportTest(unittest.TestCase):
    def test_real_dotted_task_code_allowed_without_path_traversal(self):
        self.assertEqual(b.identifier("Model_chat_GLM5.2"), "Model_chat_GLM5.2")
        for value in (".", "..", "../claim", "x/y", "x%2Fy"):
            with self.assertRaises(b.Stop):
                b.identifier(value)

    def test_endpoint_allowlist(self):
        api = b.API({}, writes=True)
        for path in (b.GROWTH + "/buddy/draw", b.GROWTH + "/tasks/accept", "https://evil.test/", b.GROWTH + "/tasks/../../evil/claim"):
            with self.assertRaises(b.Stop) as error:
                api.call("POST", path)
            self.assertEqual(error.exception.code, "ACTION_NOT_ALLOWED")

    def test_diagnose_cannot_write(self):
        with self.assertRaises(b.Stop) as error:
            b.API({}).call("POST", b.CLAIM)
        self.assertEqual(error.exception.code, "ACTION_NOT_ALLOWED")

    def test_redirect_never_carries_token(self):
        with self.assertRaises(b.Stop):
            b.NoRedirect().redirect_request(None, None, 302, None, None, "https://evil.test")

    def test_http_200_business_error_rejected(self):
        for body in ({"code": 10001}, {"success": False}, {"data": {"code": 42}}, None, "OK"):
            with self.assertRaises(b.Stop):
                b.unwrap(body)

    def test_auth_rejects_other_endpoint_and_encrypted_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "auth.json"
            for auth in ({"endpoint": "https://evil.test", "accessToken": "test"},
                         {"endpoint": b.BASE, "accessToken": {"$wbEncrypted": True}}):
                path.write_text(json.dumps({"auth": auth, "account": {"uid": "test-user"}}))
                with self.assertRaises(b.Stop):
                    b.session(path)

    def test_due_time_in_shanghai(self):
        moment = b.now().replace(hour=6, minute=29)
        self.assertFalse(b.due(moment))
        self.assertTrue(b.due(moment.replace(minute=30)))


class NotificationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name)
        (self.dir / "WorkBuddy领取提醒.app").mkdir()
        self.data_patch = patch.object(b, "DATA", self.dir)
        self.data_patch.start()

    def tearDown(self):
        self.data_patch.stop()
        self.temp.cleanup()

    def send(self, args, **kwargs):
        b.atomic(self.dir / "notification-result.json", {"request_id": args[-1], "status": "QUEUED"})

    def test_login_error_notifies_once_a_day(self):
        with patch.object(b.subprocess, "run", side_effect=self.send) as send:
            self.assertEqual(b.notify_login([{"status":"LOGIN_REJECTED"}], "scheduled"), "QUEUED")
            self.assertEqual(b.notify_login([{"status":"LOGIN_REJECTED"}], "scheduled"), "ALREADY_NOTIFIED_TODAY")
            self.assertEqual(send.call_count, 1)

    def test_read_only_and_success_and_network_are_quiet(self):
        with patch.object(b.subprocess, "run") as send:
            b.notify_login([{"status":"LOGIN_REJECTED"}], "diagnose")
            b.notify_login([{"status":"DAILY_CLAIM_VERIFIED"}], "scheduled")
            b.notify_login([{"status":"NETWORK_UNAVAILABLE"}], "scheduled")
            send.assert_not_called()

    def test_denied_notification_does_not_count_as_delivered(self):
        def denied(args, **kwargs):
            b.atomic(self.dir / "notification-result.json", {"request_id":args[-1], "status":"NOT_AUTHORIZED"})
        with patch.object(b.subprocess, "run", side_effect=denied):
            self.assertEqual(b.notify_login([{"status":"LOGIN_REQUIRED"}], "run"), "NOT_AUTHORIZED")
        self.assertNotIn("queued_day", b.load_json(self.dir / "notification-state.json"))

    def test_stale_receipt_never_claims_delivery(self):
        b.atomic(self.dir / "notification-result.json", {"request_id":"old", "status":"QUEUED"})
        with patch.object(b.subprocess, "run"):
            self.assertEqual(b.notify_login([{"status":"LOGIN_REJECTED"}], "scheduled"), "NO_RECEIPT")

    def test_sender_failure_does_not_break_rewards(self):
        with patch.object(b.subprocess, "run", side_effect=TimeoutError):
            self.assertEqual(b.notify_login([{"status":"LOGIN_REJECTED"}], "scheduled"), "SEND_FAILED")

    def test_next_day_may_remind_again(self):
        b.atomic(self.dir / "notification-state.json", {"queued_day":"2000-01-01", "last_attempt":0})
        with patch.object(b.subprocess, "run", side_effect=self.send):
            self.assertEqual(b.notify_login([{"status":"LOGIN_REJECTED"}], "scheduled"), "QUEUED")

    def test_compatibility_failure_uses_attention_not_relogin(self):
        with patch.object(b.subprocess, "run", side_effect=self.send) as send:
            self.assertEqual(b.notify_login([{"status":"ENCRYPTED_LOGIN_UNSUPPORTED"}], "scheduled"), "QUEUED")
            self.assertIn("--attention", send.call_args.args[0])
            self.assertEqual(b.notify_login([{"status":"LOGIN_DECRYPT_FAILED"}], "scheduled"), "ALREADY_NOTIFIED_TODAY")

    def test_unknown_permanent_failure_is_not_silent(self):
        with patch.object(b.subprocess, "run", side_effect=self.send) as send:
            self.assertEqual(b.notify_login([{"status":"FUTURE_FAILURE"}], "scheduled"), "QUEUED")
            self.assertIn("--attention", send.call_args.args[0])

    def test_persistent_network_failure_alerts(self):
        b.atomic(self.dir / "background-health.json", {"network_failed_since": b.time.time() - 3601})
        with patch.object(b.subprocess, "run", side_effect=self.send):
            self.assertEqual(b.notify_login([{"status":"NETWORK_UNAVAILABLE"}], "scheduled"), "QUEUED")

    def test_good_run_resets_network_streak(self):
        b.atomic(self.dir / "background-health.json", {"network_failed_since": 1})
        with patch("builtins.print"), patch.object(b, "screen_state", return_value="LOCKED"):
            b.log_result([{"status":"NO_COMPLETED_TASKS"}], "scheduled")
        self.assertNotIn("network_failed_since", b.load_json(self.dir / "background-health.json"))


class DesktopSessionTest(unittest.TestCase):
    def test_unknown_encryption_never_launches_helper(self):
        with patch.object(b.subprocess, "run") as run:
            for wrapper in ({"$wbEncrypted": 1, "scheme": "future", "envelope": "x"},
                            {"$wbEncrypted": True, "envelope": "x"}):
                with self.assertRaises(b.Stop):
                    b.desktop_token(wrapper)
            run.assert_not_called()

    def test_private_pipe_and_clean_environment(self):
        wrapper = {"$wbEncrypted": 1, "envelope": "fake-encrypted-value"}
        with patch.object(Path, "is_file", return_value=True), \
             patch.dict(b.os.environ, {"NODE_OPTIONS": "unwanted"}), \
             patch.object(b.subprocess, "run", return_value=b.subprocess.CompletedProcess([], 0, b"synthetic-token")) as run:
            self.assertEqual(b.desktop_token(wrapper), "synthetic-token")
            args, kw = run.call_args
            self.assertNotIn(wrapper["envelope"], str(args))
            self.assertNotIn("NODE_OPTIONS", kw["env"])
            self.assertEqual(kw["stdout"], b.subprocess.PIPE)
            self.assertEqual(kw["stderr"], b.subprocess.DEVNULL)

    def test_helper_failure_does_not_expose_output(self):
        with patch.object(Path, "is_file", return_value=True), \
             patch.object(b.subprocess, "run", return_value=b.subprocess.CompletedProcess([], 2, b"sensitive-test-value")):
            with self.assertRaises(b.Stop) as error:
                b.desktop_token({"$wbEncrypted": 1, "envelope": "fake"})
            self.assertEqual(str(error.exception), "LOGIN_DECRYPT_FAILED")

    def test_valid_encrypted_session_preserves_account_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "auth.json"
            data = {"auth": {"endpoint": b.BASE, "accessToken": "synthetic-token"}, "account": {"uid": "test-user"}}
            path.write_text(json.dumps(data))
            plain_headers, plain_account = b.session(path)
            data["auth"]["accessToken"] = {"$wbEncrypted": 1, "envelope": "fake"}
            path.write_text(json.dumps(data))
            with patch.object(b, "desktop_token", return_value="synthetic-token"):
                headers, account = b.session(path)
            self.assertEqual((headers, account), (plain_headers, plain_account))


if __name__ == "__main__":
    unittest.main()
