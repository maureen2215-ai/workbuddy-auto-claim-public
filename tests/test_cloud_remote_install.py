import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
s=importlib.util.spec_from_file_location('remote_installer',ROOT/'cloud/install_remote.py')
m=importlib.util.module_from_spec(s);s.loader.exec_module(m)


class RemoteInstallTest(unittest.TestCase):
    def test_failed_stage_removes_only_its_own_files_and_user(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for name in ('opt','etc','var/lib','etc/systemd/system'):(root/name).mkdir(parents=True,exist_ok=True)
            unrelated=root/'opt/other-service';unrelated.mkdir();(unrelated/'keep').write_text('keep')
            def mapped(path):return root/str(path).lstrip('/')
            def command(args):
                if args[0]=='systemd-analyze':raise RuntimeError('synthetic failure')
                return b''
            bundle={'state':{'account':'fake'},'session':{'account':'fake','headers':{'Authorization':'Bearer fake-secret'}},
                'authorization':{'expected_account':'fake'},'sources':{'background.py':'pass','cloud.py':'pass'},
                'units':{name:'synthetic unit' for name in ('workbuddy-autoclaim.service','workbuddy-autoclaim-verify.service','workbuddy-autoclaim.timer')}}
            with patch.object(m,'Path',side_effect=mapped),patch.object(m.os,'geteuid',return_value=0),\
                 patch.object(m.os,'chown'),patch.object(m.os,'umask'),patch.object(m.resource,'setrlimit'),\
                 patch.object(m.pwd,'getpwnam',side_effect=[KeyError(),SimpleNamespace(pw_uid=1,pw_gid=1)]),\
                 patch.object(m,'run',side_effect=command) as run,\
                 patch.object(m.subprocess,'run',return_value=subprocess.CompletedProcess([],3,stdout='inactive\n')):
                result=m.install(bundle)
            self.assertTrue(result['rollback_complete'])
            self.assertEqual(result['stage'],'STAGING_FAILED_NO_TIMER_ENABLED')
            self.assertFalse((root/'etc/workbuddy-auto-claim').exists())
            self.assertFalse((root/'opt/workbuddy-auto-claim').exists())
            self.assertFalse((root/'var/lib/workbuddy-auto-claim').exists())
            self.assertEqual(list((root/'etc/systemd/system').iterdir()),[])
            self.assertEqual((unrelated/'keep').read_text(),'keep')
            self.assertIn(['userdel','workbuddy-claim'],[call.args[0] for call in run.call_args_list])


if __name__=='__main__':unittest.main()
