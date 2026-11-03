#!/usr/bin/env python3
"""Private local controls. No secrets on argv, stdout, stderr, or local disk."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import resource
import re
import shlex
import subprocess
import sys
import time

RUNTIME = Path.home() / 'Library/Application Support/WorkBuddyAutoClaim'
ROOT = RUNTIME / 'cloud-control'

REMOTE_STATUS = '''import json,subprocess
from pathlib import Path
root=Path('/var/lib/workbuddy-auto-claim')
def read(name):
 p=root/name
 return json.loads(p.read_text()) if p.exists() else {}
state=read('background-state.json')
out={'source':'cloud','daily_confirmed':state.get('daily_confirmed')}
for name in ('background-latest.json','verification-latest.json','scheduler-heartbeat.json'):
 out[name]=read(name)
for unit in ('workbuddy-autoclaim.timer','workbuddy-autoclaim.service'):
 r=subprocess.run(['systemctl','show',unit,'--property=ActiveState,UnitFileState,Result,ExecMainStatus,NextElapseUSecRealtime,LastTriggerUSec'],capture_output=True,text=True,timeout=10)
 out[unit]=dict(line.split('=',1) for line in r.stdout.splitlines() if '=' in line)
events=root/'background-events.jsonl'
out['recent_events']=[json.loads(line) for line in events.read_text().splitlines()[-40:]] if events.exists() else []
print(json.dumps(out))
'''

REMOTE_REFRESH = '''import json,os,sys,resource,hashlib
from pathlib import Path
sys.path.insert(0,'/opt/workbuddy-auto-claim')
import background as b
resource.setrlimit(resource.RLIMIT_CORE,(0,0));os.umask(0o077)
try:
 raw=sys.stdin.buffer.read(65537)
 if len(raw)>65536:raise ValueError()
 candidate=json.loads(raw)
 state=b.load_json(Path('/var/lib/workbuddy-auto-claim/background-state.json'))
 config=b.load_json(Path('/etc/workbuddy-auto-claim/authorization.json'))
 headers=candidate['headers']
 calculated=hashlib.sha256((headers['X-User-Id']+'|'+headers.get('X-Enterprise-Id','')+'|'+headers.get('X-Domain','')).encode()).hexdigest()[:24]
 if candidate['account']!=calculated or calculated!=state['account'] or calculated!=config.get('expected_account') or headers.get('X-Tenant-Id','')!=headers.get('X-Enterprise-Id',''):raise b.Stop('ACCOUNT_CHANGED_REVIEW_REQUIRED')
 api=b.API(headers,writes=False)
 status=api.call('POST',b.STATUS)
 checked,active=b.daily_status(status)
 b.atomic(Path('/etc/workbuddy-auto-claim/session.json'),candidate)
 print(json.dumps({'status':'CREDENTIAL_REFRESHED','checked':checked,'active':active}))
except b.Stop as e:print(json.dumps({'status':e.code}));sys.exit(1)
except Exception:print(json.dumps({'status':'CREDENTIAL_REFRESH_FAILED'}));sys.exit(1)
'''


def local_module():
    source = ROOT / 'background.py'
    if not source.is_file():
        source = RUNTIME / 'background/background.py'
    spec = importlib.util.spec_from_file_location('local_workbuddy', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def remote(code, payload=None, timeout=45):
    host = json.loads((ROOT / 'server.private.json').read_text())['host']
    if not isinstance(host, str) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.@-]{0,253}', host):
        raise RuntimeError('INVALID_SSH_TARGET')
    proc = subprocess.run(['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
        '-o', 'ConnectTimeout=10', host, 'sudo -n python3 -B -c ' + shlex.quote(code)],
        input=json.dumps(payload).encode() if payload is not None else b'',
        capture_output=True, timeout=timeout)
    try:
        result = json.loads(proc.stdout)
        if not isinstance(result, dict): raise ValueError()
    except Exception:
        raise RuntimeError('CLOUD_STATUS_UNAVAILABLE') from None
    return result


def refresh():
    module = local_module()
    module.authorization(RUNTIME / 'background-authorization.json')
    headers, account = module.session(module.AUTH)
    # The remote side independently compares identity and checks the candidate
    # against the service before replacing the root-only credential file.
    return remote(REMOTE_REFRESH, {'headers': headers, 'account': account})


def monitor():
    module = local_module()
    try:
        value = remote(REMOTE_STATUS)
        latest = value.get('background-latest.json', {})
        rows = [r for r in latest.get('results', []) if r.get('status') not in module.GOOD]
        timer = value.get('workbuddy-autoclaim.timer', {})
        if timer.get('ActiveState') != 'active': rows.append({'status': 'CLOUD_TIMER_NOT_ACTIVE'})
        service = value.get('workbuddy-autoclaim.service', {})
        if service.get('Result') not in (None, '', 'success') and not rows:
            rows.append({'status': 'CLOUD_RUN_FAILED'})
        # No success record for today is actionable after the morning window.
        now = module.now()
        stamps = [entry.get('time') for entry in (latest, value.get('scheduler-heartbeat.json', {}))]
        recent = [module.dt.datetime.fromisoformat(stamp) for stamp in stamps if stamp]
        if now.hour >= 7 and (not recent or (now-max(recent)).total_seconds() > 1200) and not rows:
            rows.append({'status': 'CLOUD_SCHEDULER_STALE'})
        if now.hour >= 8 and value.get('daily_confirmed') != now.date().isoformat() and not rows:
            rows.append({'status': 'CLOUD_TODAY_NOT_VERIFIED'})
        result = {'time': now.isoformat(), 'source': 'cloud-monitor',
                  'daily_confirmed': value.get('daily_confirmed'), 'results': rows}
        health = module.load_json(RUNTIME / 'cloud-monitor-health.json', {})
        health.pop('unreachable_since', None)
    except Exception:
        health = module.load_json(RUNTIME / 'cloud-monitor-health.json', {})
        health.setdefault('unreachable_since', time.time())
        rows = [{'status': 'CLOUD_MONITOR_UNREACHABLE'}] if time.time()-health['unreachable_since'] >= 3600 else []
        result = {'time': module.now().isoformat(), 'source': 'cloud-monitor',
                  'status': 'CLOUD_STATUS_UNAVAILABLE', 'results': rows}
    module.atomic(RUNTIME / 'cloud-monitor-health.json', health)
    result['notification'] = module.notify_login(rows, 'scheduled') if rows else 'NOT_NEEDED'
    module.atomic(RUNTIME / 'cloud-monitor-latest.json', result)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=('status', 'monitor', 'refresh', 'verify'))
    args = parser.parse_args()
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    os.umask(0o077)
    try:
        if args.mode == 'status': result = remote(REMOTE_STATUS)
        elif args.mode == 'monitor': result = monitor()
        elif args.mode == 'refresh': result = refresh()
        else:
            code = "import subprocess,json; from pathlib import Path; r=subprocess.run(['systemctl','start','workbuddy-autoclaim-verify.service'],capture_output=True,timeout=30); print((Path('/var/lib/workbuddy-auto-claim/verification-latest.json').read_text()) if r.returncode==0 else json.dumps({'status':'VERIFY_FAILED'}))"
            result = remote(code)
        print(json.dumps(result, ensure_ascii=False))
        return 1 if result.get('status') in ('CREDENTIAL_REFRESH_FAILED','ACCOUNT_CHANGED_REVIEW_REQUIRED','LOGIN_REJECTED','VERIFY_FAILED') else 0
    except Exception:
        print(json.dumps({'status': 'CLOUD_CONTROL_FAILED'}))
        return 1


if __name__ == '__main__': raise SystemExit(main())
