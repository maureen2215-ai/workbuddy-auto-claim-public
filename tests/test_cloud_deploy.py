import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'cloud'))
spec=importlib.util.spec_from_file_location('cloud_deploy',ROOT/'cloud/deploy.py')
d=importlib.util.module_from_spec(spec);spec.loader.exec_module(d)


class CloudDeployTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.data=self.root/'runtime';self.data.mkdir()
        self.bridge=self.data/'cloud-control'
        self.patches=[patch.object(d.b,'DATA',self.data),patch.object(d.control,'ROOT',self.bridge),
                      patch.object(d.sys,'platform','darwin')]
        for p in self.patches:p.start()
    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.tmp.cleanup()
    def test_preview_never_reads_credentials_or_calls_ssh(self):
        with patch.object(d.b,'session') as session,patch.object(d,'ssh') as ssh,contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(d.main([]),0)
        session.assert_not_called();ssh.assert_not_called()
        self.assertFalse(self.bridge.exists())
    def test_stage_requires_specific_upload_consent(self):
        with patch.object(d.b,'session') as session:
            with self.assertRaises(d.b.Stop):d.main(['--stage','--host','example-host'])
        session.assert_not_called()
    def test_existing_cloud_target_is_preserved(self):
        self.bridge.mkdir();(self.bridge/'server.private.json').write_text('{}')
        with patch.object(d.b,'session') as session:
            with self.assertRaises(d.b.Stop):d.stage('example-host')
        session.assert_not_called()
    def test_stage_only_saves_safe_local_control_files(self):
        d.b.atomic(self.data/'background-authorization.json',{'use_desktop_session':True,'endpoint':d.b.BASE,'daily':True})
        headers={'Authorization':'Bearer synthetic-secret','X-User-Id':'synthetic-user'}
        with patch.object(d.b,'session',return_value=(headers,'synthetic-account')),\
             patch.object(d.b,'API') as api,patch.object(d,'ssh',return_value={'stage':'INSTALLED_TIMER_NOT_ENABLED'}) as ssh:
            api.return_value.call.return_value={'today_checked_in':True,'active':True}
            d.stage('example-host')
            bundle=ssh.call_args.args[2]
            self.assertEqual(bundle['authorization']['expected_account'],'synthetic-account')
            self.assertTrue(bundle['authorization']['use_cloud_session'])
            self.assertNotIn('use_desktop_session',bundle['authorization'])
        for path in self.bridge.iterdir():
            self.assertNotIn('synthetic-secret',path.read_text())
    def test_account_change_rejected_before_network(self):
        d.b.atomic(self.data/'background-state.json',{'account':'old-account','actions':{}})
        with patch.object(d.b,'API') as api:
            with self.assertRaises(d.b.Stop):d.identity_state({},'other-account')
        api.assert_not_called()
    def test_local_stop_is_verified_before_enabling_remote(self):
        (self.data/'enabled').touch();backup=self.root/'backup';backup.mkdir()
        def fake(args,**kwargs):
            # Simulate a service that refused to unload.
            return subprocess.CompletedProcess(args,0)
        with patch.object(d.subprocess,'run',side_effect=fake):
            with self.assertRaises(d.b.Stop):d.stop_local(backup)
        self.assertTrue((self.data/'enabled').exists())
    def test_uncertain_activation_never_restarts_local(self):
        self.bridge.mkdir();d.b.atomic(self.bridge/'server.private.json',{'host':'example-host'})
        order=[]
        with patch.object(d.b,'session',return_value=({},'test-account')),\
             patch.object(d,'identity_state',return_value={'account':'test-account','actions':{}}),\
             patch.object(d,'stop_local',side_effect=lambda _:order.append('local-stopped')),\
             patch.object(d,'ssh',side_effect=lambda *a,**k:(_ for _ in ()).throw(d.b.Stop('REMOTE_OUTCOME_UNKNOWN'))):
            with self.assertRaises(d.b.Stop):d.activate()
        self.assertEqual(order,['local-stopped'])
        self.assertEqual(d.b.load_json(self.data/'execution-owner.json')['source'],'cloud')
    def test_repeated_activation_blocked(self):
        self.bridge.mkdir();d.b.atomic(self.bridge/'server.private.json',{'host':'example-host'})
        d.b.atomic(self.data/'execution-owner.json',{'source':'cloud'})
        with patch.object(d,'ssh') as ssh:
            with self.assertRaises(d.b.Stop):d.activate()
        ssh.assert_not_called()
    def test_host_option_injection_rejected(self):
        with patch.object(d.subprocess,'run') as run:
            with self.assertRaises(d.b.Stop):d.ssh('-oProxyCommand=bad','pass',{})
        run.assert_not_called()

    def test_initial_remote_failure_returns_error_and_keeps_owner(self):
        self.bridge.mkdir();d.b.atomic(self.bridge/'server.private.json',{'host':'example-host'})
        with patch.object(d.b,'session',return_value=({},'test-account')),\
             patch.object(d,'identity_state',return_value={'account':'test-account','actions':{}}),\
             patch.object(d,'stop_local'),\
             patch.object(d,'ssh',return_value={'timer_enabled':True,'initial_service_exit':1}):
            with self.assertRaises(d.b.Stop) as error:d.activate()
        self.assertEqual(error.exception.code,'CLOUD_INITIAL_RUN_FAILED_LOCAL_REMAINS_STOPPED')
        self.assertEqual(d.b.load_json(self.data/'execution-owner.json')['source'],'cloud')
        self.assertEqual(d.b.load_json(self.bridge/'activation.local.json')['initial_service_exit'],1)

    def test_local_claim_rechecks_owner_after_acquiring_lock(self):
        with patch.object(d.b.sys,'argv',['background.py','scheduled']),\
             patch.object(d.b,'load_json',side_effect=[{}, {'source':'cloud'}]),\
             patch.object(d.b,'session') as session,contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(d.b.main(),1)
        session.assert_not_called()


if __name__=='__main__':unittest.main()
