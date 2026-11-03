#!/usr/bin/env python3
"""Explicit stage/activate workflow. No credentials are read by preview."""
import argparse
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import re
import resource
import shutil
import subprocess
import sys
import shlex

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'src'))
import background as b
import cloud_control as control

UNITS = ('workbuddy-autoclaim.service', 'workbuddy-autoclaim-verify.service', 'workbuddy-autoclaim.timer')


def ssh(host, code, bundle, timeout=240):
    if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.@-]{0,253}', host):
        raise b.Stop('INVALID_SSH_TARGET')
    proc = subprocess.run(['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
        '-o', 'ConnectTimeout=10', host, 'sudo -n python3 -B -c ' + shlex.quote(code)],
        input=json.dumps(bundle).encode(), capture_output=True, timeout=timeout)
    try:
        result = json.loads(proc.stdout)
    except Exception:
        raise b.Stop('REMOTE_OUTCOME_UNKNOWN') from None
    if isinstance(result, dict) and result.get('stage') == 'STAGING_FAILED_NO_TIMER_ENABLED':
        return result
    if proc.returncode or not isinstance(result, dict):
        raise b.Stop('REMOTE_OPERATION_FAILED')
    return result


def identity_state(headers, account):
    store = b.Store(b.DATA / 'background-state.json', account)
    # Account mismatch is checked before credentials ever leave the Mac.
    b.daily_status(b.API(headers, writes=False).call('POST', b.STATUS))
    return store.data


def stage(host):
    if sys.platform != 'darwin': raise b.Stop('MACOS_REQUIRED_FOR_DESKTOP_AUTH')
    if control.ROOT.exists():
        raise b.Stop('EXISTING_CLOUD_CONFIGURATION_PRESERVED')
    config = b.authorization(b.DATA / 'background-authorization.json')
    with (b.DATA / 'run.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        headers, account = b.session(b.AUTH)
        state = identity_state(headers, account)
        config = dict(config, use_cloud_session=True, expected_account=account)
        config.pop('use_desktop_session', None)
        bundle = {'sources': {name: (REPO / 'src' / name).read_text() for name in ('background.py','cloud.py')},
                  'units': {name: (REPO / 'cloud' / name).read_text() for name in UNITS},
                  'state': state, 'authorization': config,
                  'session': {'headers': headers, 'account': account}}
        # Save the non-secret recovery pointer before transmitting credentials.
        # If SSH is interrupted, the target is still known locally for review.
        control.ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
        b.atomic(control.ROOT / 'server.private.json', {'host': host})
        for name in ('background.py', 'desktop_session.cjs'):
            shutil.copy2(REPO / 'src' / name, control.ROOT / name)
            (control.ROOT / name).chmod(0o600)
        shutil.copy2(REPO / 'cloud/cloud_control.py', control.ROOT / 'cloud_control.py')
        b.atomic(control.ROOT / 'stage.local.json', {'stage': 'STAGING_OUTCOME_PENDING'})
        result = ssh(host, (REPO / 'cloud/install_remote.py').read_text(), bundle)
        b.atomic(control.ROOT / 'stage.local.json', result)
        if result.get('stage') != 'INSTALLED_TIMER_NOT_ENABLED':
            raise b.Stop('REMOTE_STAGE_UNVERIFIED')
        return result


ACTIVATE = '''import fcntl,json,os,pwd,subprocess,sys
from pathlib import Path
sys.path.insert(0,'/opt/workbuddy-auto-claim')
import background as b
os.umask(0o077)
root=Path('/var/lib/workbuddy-auto-claim')
state=json.load(sys.stdin)
config=b.load_json(Path('/etc/workbuddy-auto-claim/authorization.json'))
assert state['account']==config['expected_account']
for unit in ('workbuddy-autoclaim.timer','workbuddy-autoclaim.service'):
 r=subprocess.run(['systemctl','is-active',unit],capture_output=True,text=True)
 assert r.stdout.strip() not in ('active','activating')
user=pwd.getpwnam('workbuddy-claim')
with (root/'run.lock').open('a') as lock:
 fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
 b.atomic(root/'migration-state-before.json',b.load_json(root/'background-state.json'))
 b.atomic(root/'background-state.json',state)
 os.chown(root/'background-state.json',user.pw_uid,user.pw_gid)
 os.chown(root/'run.lock',user.pw_uid,user.pw_gid)
r=subprocess.run(['systemctl','enable','--now','workbuddy-autoclaim.timer'],capture_output=True,timeout=30)
assert r.returncode==0
r=subprocess.run(['systemctl','start','workbuddy-autoclaim.service'],capture_output=True,timeout=190)
print(json.dumps({'source':'cloud','timer_enabled':True,'initial_service_exit':r.returncode}))
'''


def stop_local(backup):
    target = f'gui/{os.getuid()}/local.workbuddy.autoclaim'
    subprocess.run(['launchctl', 'disable', target], check=True, capture_output=True)
    subprocess.run(['launchctl', 'bootout', target], capture_output=True)
    if subprocess.run(['launchctl', 'print', target], capture_output=True).returncode == 0:
        raise b.Stop('LOCAL_STILL_RUNNING')
    if (b.DATA / 'enabled').exists(): (b.DATA / 'enabled').rename(backup / 'enabled')


def activate():
    config = b.load_json(control.ROOT / 'server.private.json')
    if b.load_json(b.DATA / 'execution-owner.json', {}).get('source') == 'cloud':
        raise b.Stop('CLOUD_ALREADY_PRIMARY')
    with (b.DATA / 'run.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        headers, account = b.session(b.AUTH)
        state = identity_state(headers, account)
        backup = b.DATA / 'backups' / ('cloud-cutover-' + b.now().strftime('%Y%m%d-%H%M%S-%f'))
        backup.mkdir(parents=True, mode=0o700)
        b.atomic(backup / 'background-state.json', state)
        plist = Path.home() / 'Library/LaunchAgents/local.workbuddy.autoclaim.plist'
        if plist.exists(): shutil.copy2(plist, backup / plist.name)
        stop_local(backup)
        # On uncertain remote results leave local blocked until both remote units
        # are proven stopped. Never automatically re-enable the old claimant.
        b.atomic(b.DATA / 'execution-owner.json', {'source': 'cloud', 'since': b.now().isoformat()})
        result = ssh(config['host'], ACTIVATE, state)
        b.atomic(control.ROOT / 'activation.local.json', result)
        if result.get('initial_service_exit') != 0:
            raise b.Stop('CLOUD_INITIAL_RUN_FAILED_LOCAL_REMAINS_STOPPED')
        return result


def install_monitor():
    bridge = control.ROOT / 'cloud_control.py'
    if not bridge.is_file(): raise b.Stop('CLOUD_STAGE_REQUIRED')
    helper = b.DATA / 'WorkBuddy领取提醒.app'
    if not helper.is_dir():
        built = REPO / 'WorkBuddy领取提醒.app'
        if not built.is_dir(): raise b.Stop('BUILD_NOTIFIER_FIRST')
        subprocess.run(['/usr/bin/codesign','--verify','--deep','--strict',str(built)],check=True,capture_output=True)
        shutil.copytree(built, helper, symlinks=True)
    path = Path.home() / 'Library/LaunchAgents/local.workbuddy.cloud-monitor.plist'
    if path.exists(): raise b.Stop('EXISTING_MONITOR_PRESERVED')
    config = {'Label': 'local.workbuddy.cloud-monitor',
              'ProgramArguments': [sys.executable, '-B', str(bridge), 'monitor'],
              'RunAtLoad': True, 'StartInterval': 1800,
              'StandardOutPath': str(b.DATA / 'cloud-monitor-launcher.log'),
              'StandardErrorPath': str(b.DATA / 'cloud-monitor-error.log')}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps(config)); path.chmod(0o600)
    subprocess.run(['launchctl','bootstrap',f'gui/{os.getuid()}',str(path)],check=True,capture_output=True)
    return {'monitor_installed': True, 'notification_permission_requires_user_check': True}


def main(argv=None):
    p = argparse.ArgumentParser(description='云端方案默认预览；stage 保存凭证但不启用领取；activate 切换执行端')
    p.add_argument('--host', help='本人服务器的 SSH 别名；已核验主机密钥，支持无交互 sudo')
    group = p.add_mutually_exclusive_group()
    group.add_argument('--stage', action='store_true')
    group.add_argument('--activate', action='store_true')
    group.add_argument('--install-monitor', action='store_true')
    p.add_argument('--authorize-cloud-session', action='store_true', help='明确允许所选服务器长期保存本人登录凭证')
    args = p.parse_args(argv)
    if args.stage and (not args.host or not args.authorize_cloud_session):
        raise b.Stop('EXPLICIT_CLOUD_AUTHORIZATION_REQUIRED')
    if not (args.stage or args.activate or args.install_monitor):
        print(json.dumps({'mode':'preview','requires':'Linux, Python >=3.10, systemd >=249, SSH known host, sudo',
            'credential_storage':'root-owned 0600 on chosen server','local_claim_will_stop_on_activate':True,
            'automatic_login_renewal':False,'cloud_port':None},ensure_ascii=False))
        return 0
    if sys.platform != 'darwin': raise b.Stop('MACOS_REQUIRED_FOR_SETUP')
    resource.setrlimit(resource.RLIMIT_CORE,(0,0)); os.umask(0o077)
    result = stage(args.host) if args.stage else activate() if args.activate else install_monitor()
    print(json.dumps(result,ensure_ascii=False))
    return 0


if __name__ == '__main__':
    try: sys.exit(main())
    except Exception as e:
        print(json.dumps({'error':getattr(e,'code','CLOUD_SETUP_FAILED'),
            'hint':'保留状态；若已切换，确认云端停止后才能恢复本机。不要提交或输出凭证。'},ensure_ascii=False))
        sys.exit(1)
