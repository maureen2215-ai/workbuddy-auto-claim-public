"""Setup tests use temporary paths and fake official/launchd calls only."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / (name + ".py"))
    result = importlib.util.module_from_spec(spec); spec.loader.exec_module(result); return result


c = module("configure")
i = module("install_background")
m = module("manage")
b = c.b


class LocalToolsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name); self.data = self.base / "data"
        self.auth = self.data / "background-authorization.json"

    def tearDown(self): self.temp.cleanup()

    def test_preview_never_creates_data_or_reads_session(self):
        with patch.object(b, "DATA", self.data), patch.object(b, "session") as session, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(c.main([]), 0)
        self.assertFalse(self.data.exists()); session.assert_not_called()

    def test_daily_authorization_has_no_optional_scopes_or_network(self):
        with patch.object(b, "DATA", self.data), patch.object(c.sys, "platform", "darwin"), \
             patch.object(b, "session") as session, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(c.main(["--authorize-daily"]), 0)
        config = json.loads(self.auth.read_text())
        self.assertTrue(config["daily"]); self.assertFalse(config["travel"])
        self.assertFalse(config["completed_tasks"]); self.assertFalse((self.data / "enabled").exists())
        session.assert_not_called()

    def test_existing_authorization_is_not_overwritten(self):
        self.data.mkdir(); self.auth.write_text('{"preserve":true}')
        with patch.object(b, "DATA", self.data), patch.object(c.sys, "platform", "darwin"):
            with self.assertRaises(b.Stop) as error: c.main(["--authorize-daily"])
        self.assertEqual(error.exception.code, "EXISTING_AUTHORIZATION_PRESERVED")
        self.assertEqual(self.auth.read_text(), '{"preserve":true}')

    def test_paid_or_unconfirmed_travel_never_saves_approval(self):
        class API:
            def __init__(self, location): self.location = location
            def call(self, method, path):
                assert method == "GET"; return {"locations": [self.location]}
        for loc, free in [({"id": 1, "cost": 10}, True), ({"id": 1}, False)]:
            with self.assertRaises(b.Stop): c.reviewed_location(API(loc), "1", free)

    def test_reviewed_location_pins_full_config_fingerprint(self):
        class API:
            def call(self, method, path): return {"locations": [{"id": 1, "name": "fake cafe", "cost": 0}]}
        result = c.reviewed_location(API(), "1", True)
        self.assertTrue(result["travel"]); self.assertEqual(len(result["travel_location_fingerprint"]), 64)

    def test_pause_preview_does_not_run_launchctl(self):
        with patch.object(m, "DATA", self.data), patch.object(m.subprocess, "run") as run, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(m.main(["pause"]), 0)
        run.assert_not_called(); self.assertFalse(self.data.exists())

    def test_pause_preserves_state_and_authorization(self):
        self.data.mkdir(); (self.data / "enabled").touch()
        self.auth.write_text("keep-auth"); (self.data / "background-state.json").write_text("keep-state")
        with patch.object(m, "DATA", self.data), patch.object(m.sys, "platform", "darwin"), \
             patch.object(m.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout="")), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(m.main(["pause", "--apply"]), 0)
        self.assertFalse((self.data / "enabled").exists())
        self.assertEqual(self.auth.read_text(), "keep-auth")
        self.assertEqual((self.data / "background-state.json").read_text(), "keep-state")

    def test_install_preview_does_not_read_login_or_launchctl(self):
        with patch.object(i, "DATA", self.data), patch.object(b, "session") as session, \
             patch.object(i, "run") as run, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(i.main([]), 0)
        session.assert_not_called(); run.assert_not_called(); self.assertFalse(self.data.exists())


class InstallTransactionTest(unittest.TestCase):
    setUp = LocalToolsTest.setUp
    tearDown = LocalToolsTest.tearDown
    def prepare(self):
        root = self.base / "repo"; (root / "src").mkdir(parents=True)
        for name in i.NAMES: (root / "src" / name).write_text("new-" + name)
        (root / i.APP).mkdir(); (root / i.APP / "binary").write_text("new-app")
        self.data.mkdir(); b.atomic(self.auth, c.daily_config())
        runtime = self.data / "background"; runtime.mkdir()
        for name in i.NAMES: (runtime / name).write_text("old-" + name)
        (self.data / i.APP).mkdir(); (self.data / i.APP / "binary").write_text("old-app")
        self.state = self.data / "background-state.json"; self.state.write_text('{"account":"synthetic-account","actions":{}}')
        plist = self.base / "LaunchAgents" / "job.plist"; plist.parent.mkdir(); plist.write_text("old-plist")
        return root, runtime, plist

    def fake_api(self, headers):
        class API:
            def call(self, method, path):
                assert (method, path) == ("POST", b.STATUS)
                return {"active": True, "today_checked_in": True}
        return API()

    def execute(self, fail=False):
        root, runtime, plist = self.prepare(); calls = []; failed = False
        original_auth = self.auth.read_bytes(); original_state = self.state.read_bytes()
        def fake_run(command, **kwargs):
            nonlocal failed
            calls.append(command)
            if fail and "bootstrap" in command and not failed:
                failed = True; raise subprocess.CalledProcessError(1, command)
            return subprocess.CompletedProcess(command, 0)
        with patch.object(i, "ROOT", root), patch.object(i, "DATA", self.data), patch.object(i, "PLIST", plist), \
             patch.object(i.sys, "platform", "darwin"), patch.object(i, "run", side_effect=fake_run), \
             patch.object(b, "session", return_value=({}, "synthetic-account")), \
             patch.object(b, "API", side_effect=self.fake_api), contextlib.redirect_stdout(io.StringIO()):
            if fail:
                with self.assertRaises(subprocess.CalledProcessError): i.install("/fake/python")
            else: self.assertEqual(i.install("/fake/python"), 0)
        self.assertEqual(self.auth.read_bytes(), original_auth); self.assertEqual(self.state.read_bytes(), original_state)
        return runtime, plist, calls

    def test_success_installs_pair_and_app_without_touching_reward_state(self):
        runtime, plist, calls = self.execute()
        for name in i.NAMES: self.assertEqual((runtime / name).read_text(), "new-" + name)
        self.assertEqual((self.data / i.APP / "binary").read_text(), "new-app")
        self.assertTrue((self.data / "enabled").exists())
        self.assertTrue(any("bootstrap" in cmd for cmd in calls))

    def test_bootstrap_failure_restores_prior_files_and_loaded_job(self):
        runtime, plist, calls = self.execute(fail=True)
        for name in i.NAMES: self.assertEqual((runtime / name).read_text(), "old-" + name)
        self.assertEqual((self.data / i.APP / "binary").read_text(), "old-app")
        self.assertEqual(plist.read_text(), "old-plist"); self.assertFalse((self.data / "enabled").exists())
        self.assertEqual(sum("bootstrap" in cmd for cmd in calls), 2)
