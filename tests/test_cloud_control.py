import datetime
import importlib.util
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('cloud_control_tested',ROOT/'cloud/cloud_control.py')
c=importlib.util.module_from_spec(spec);spec.loader.exec_module(c)


class MonitorTest(unittest.TestCase):
    def setUp(self):
        self.now=datetime.datetime(2026,10,29,8,0,tzinfo=datetime.timezone(datetime.timedelta(hours=8)))
        self.module=SimpleNamespace(now=lambda:self.now,dt=datetime,
            GOOD={'DAILY_CLAIM_VERIFIED'},load_json=lambda *args:{},atomic=Mock(),
            notify_login=Mock(return_value='QUEUED'))
        self.status={'daily_confirmed':'2026-10-29',
            'workbuddy-autoclaim.timer':{'ActiveState':'active'},
            'workbuddy-autoclaim.service':{'Result':'success'},
            'scheduler-heartbeat.json':{'time':self.now.isoformat()}}

    def run_monitor(self,status):
        with patch.object(c,'local_module',return_value=self.module),patch.object(c,'remote',return_value=status):
            return c.monitor()

    def test_healthy_monitor_never_needs_local_session(self):
        result=self.run_monitor(self.status)
        self.assertEqual(result['results'],[])
        self.module.notify_login.assert_not_called()

    def test_today_missing_is_not_treated_as_success(self):
        self.status['daily_confirmed']='2026-10-28'
        result=self.run_monitor(self.status)
        self.assertEqual(result['results'],[{'status':'CLOUD_TODAY_NOT_VERIFIED'}])

    def test_inactive_timer_is_reported_even_if_day_claimed(self):
        self.status['workbuddy-autoclaim.timer']['ActiveState']='inactive'
        result=self.run_monitor(self.status)
        self.assertIn({'status':'CLOUD_TIMER_NOT_ACTIVE'},result['results'])

    def test_login_rejection_reaches_existing_notification_channel(self):
        self.status['background-latest.json']={'time':self.now.isoformat(),'results':[{'status':'LOGIN_REJECTED'}]}
        result=self.run_monitor(self.status)
        self.assertEqual(result['notification'],'QUEUED')
        self.module.notify_login.assert_called_once_with([{'status':'LOGIN_REJECTED'}],'scheduled')

    def test_stale_scheduler_is_not_hidden_by_active_timer(self):
        self.status['scheduler-heartbeat.json']['time']=(self.now-datetime.timedelta(hours=1)).isoformat()
        result=self.run_monitor(self.status)
        self.assertIn({'status':'CLOUD_SCHEDULER_STALE'},result['results'])


if __name__=='__main__':unittest.main()
