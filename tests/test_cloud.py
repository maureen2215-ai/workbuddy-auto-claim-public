import importlib.util
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import cloud as c


class CloudTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.account = hashlib.sha256(b'fake-user||').hexdigest()[:24]
        self.config = self.root / 'config.json'
        self.config.write_text(json.dumps({'endpoint': c.b.BASE, 'use_cloud_session': True, 'daily': True, 'expected_account': self.account}))
        self.headers = {'Authorization': 'Bearer fake-token', 'X-User-Id': 'fake-user'}
        (self.root / 'session.json').write_text(json.dumps({'headers': self.headers, 'account': self.account}))
        self.patches = [patch.object(c, 'DATA', self.root), patch.object(c, 'CONFIG', self.config),
                        patch.dict(c.os.environ, {'CREDENTIALS_DIRECTORY': str(self.root)}), patch('builtins.print')]
        for p in self.patches: p.start()

    def tearDown(self):
        for p in reversed(self.patches): p.stop()
        self.tmp.cleanup()

    def test_credential_is_validated_and_account_changes_rejected(self):
        self.assertEqual(c.credential(), (self.headers, self.account))
        (self.root / 'session.json').write_text(json.dumps({'headers': self.headers, 'account': 'other'}))
        with self.assertRaises(c.b.Stop): c.credential()

    def test_verify_never_writes_rewards_or_ledger(self):
        with patch.object(c.b, 'API') as api:
            api.return_value.call.return_value = {'today_checked_in': True, 'active': True, 'today_credit': 100}
            self.assertEqual(c.execute('verify'), 0)
            api.assert_called_once_with(self.headers, writes=False)
            api.return_value.call.assert_called_once_with('POST', c.b.STATUS)
        self.assertFalse((self.root / 'background-state.json').exists())

    def test_confirmed_day_skips_all_calls_when_growth_not_due(self):
        c.b.atomic(self.root / 'background-state.json', {'account': self.account, 'actions': {},
            'daily_confirmed': c.b.now().date().isoformat(), 'next_growth': c.time.time()+3600})
        with patch.object(c.b, 'API') as api:
            self.assertEqual(c.execute('scheduled'), 0)
            api.assert_not_called()

    def test_next_day_claim_requires_server_confirmation(self):
        with patch.object(c.b, 'due', return_value=True), patch.object(c.b, 'API') as api:
            api.return_value.call.side_effect = [
                {'today_checked_in': False, 'active': True}, {},
                {'today_checked_in': True, 'active': True, 'today_credit': 100}]
            self.assertEqual(c.execute('scheduled'), 0)
            self.assertEqual(api.return_value.call.call_count, 3)
        state=c.b.load_json(self.root / 'background-state.json')
        self.assertEqual(state['daily_confirmed'], c.b.now().date().isoformat())

    def test_login_rejection_records_failure_with_retry(self):
        with patch.object(c.b, 'due', return_value=True), patch.object(c.b, 'API') as api:
            api.return_value.call.side_effect = c.b.Stop('LOGIN_REJECTED')
            self.assertEqual(c.execute('scheduled'), 1)
        state=c.b.load_json(self.root / 'background-state.json')
        self.assertGreater(state['retry_after'], c.time.time()+3500)
        text=(self.root / 'background-latest.json').read_text()
        self.assertNotIn('fake-token', text)
        self.assertNotIn('fake-user', text)

    def test_wrong_bound_account_stops_before_api(self):
        c.b.atomic(self.root / 'background-state.json', {'account': 'other', 'actions': {}})
        with patch.object(c.b, 'API') as api:
            self.assertEqual(c.execute('scheduled'), 1)
            api.assert_not_called()

    def test_missing_ledger_cannot_accept_different_authorized_identity(self):
        config=json.loads(self.config.read_text());config['expected_account']='other'
        self.config.write_text(json.dumps(config))
        with patch.object(c.b, 'API') as api:
            self.assertEqual(c.execute('scheduled'), 1)
            api.assert_not_called()

    def test_inconsistent_tenant_header_stops(self):
        headers=dict(self.headers, **{'X-Tenant-Id':'unexpected'})
        (self.root/'session.json').write_text(json.dumps({'headers':headers,'account':self.account}))
        with self.assertRaises(c.b.Stop):c.credential()


if __name__ == '__main__': unittest.main()
