"""Stage an explicitly authorized new installation; do not enable the timer."""
import json
import os
from pathlib import Path
import pwd
import resource
import shutil
import subprocess
import sys

def run(args):
    result = subprocess.run(args, capture_output=True, timeout=30)
    if result.returncode:
        raise RuntimeError('REMOTE_COMMAND_FAILED')
    return result.stdout

def write(path, value, mode=0o600):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    os.fchmod(fd, mode)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(value)
    except Exception:
        path.unlink(missing_ok=True)
        raise

def rollback(paths, units, created_user):
    """Remove only objects created by this failed first installation."""
    ok = True
    if 'workbuddy-autoclaim-verify.service' in units:
        active = subprocess.run(['systemctl', 'is-active', 'workbuddy-autoclaim-verify.service'], capture_output=True, text=True, timeout=15)
        if active.stdout.strip() in ('active', 'activating', 'deactivating', 'reloading'):
            result = subprocess.run(['systemctl', 'stop', 'workbuddy-autoclaim-verify.service'], capture_output=True, timeout=30)
            if result.returncode:
                return False
    for name in units:
        try: (Path('/etc/systemd/system') / name).unlink()
        except Exception: ok = False
    try: run(['systemctl', 'daemon-reload'])
    except Exception: ok = False
    for path in reversed(paths):
        try: shutil.rmtree(path)
        except Exception: ok = False
    if created_user:
        try: run(['userdel', 'workbuddy-claim'])
        except Exception: ok = False
    return ok


def install(bundle):
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    os.umask(0o077)
    assert os.geteuid() == 0
    paths = [Path('/opt/workbuddy-auto-claim'), Path('/etc/workbuddy-auto-claim'), Path('/var/lib/workbuddy-auto-claim')]
    assert all(not p.exists() for p in paths)
    units = ['workbuddy-autoclaim.service', 'workbuddy-autoclaim-verify.service', 'workbuddy-autoclaim.timer']
    assert all(not (Path('/etc/systemd/system') / name).exists() for name in units)
    try:
        pwd.getpwnam('workbuddy-claim')
    except KeyError:
        pass
    else:
        raise RuntimeError('USER_ALREADY_EXISTS')
    assert bundle['state']['account'] == bundle['session']['account']
    assert bundle['authorization']['expected_account'] == bundle['session']['account']
    created_paths, created_units, created_user = [], [], False
    try:
        run(['useradd', '--system', '--user-group', '--home-dir', '/var/lib/workbuddy-auto-claim', '--no-create-home', '--shell', '/usr/sbin/nologin', 'workbuddy-claim'])
        created_user = True
        user = pwd.getpwnam('workbuddy-claim')
        for p in paths:
            p.mkdir(mode=0o700)
            created_paths.append(p)
        paths[0].chmod(0o755)
        paths[1].chmod(0o711)
        for name in ('background.py', 'cloud.py'):
            write(paths[0] / name, bundle['sources'][name].encode(), 0o644)
        write(paths[1] / 'session.json', json.dumps(bundle['session']).encode())
        write(paths[1] / 'authorization.json', json.dumps(bundle['authorization']).encode(), 0o644)
        write(paths[2] / 'background-state.json', json.dumps(bundle['state']).encode())
        os.chown(paths[2], user.pw_uid, user.pw_gid)
        os.chown(paths[2] / 'background-state.json', user.pw_uid, user.pw_gid)
        for name in units:
            write(Path('/etc/systemd/system') / name, bundle['units'][name].encode(), 0o644)
            created_units.append(name)
        run(['systemd-analyze', 'verify'] + [str(Path('/etc/systemd/system') / name) for name in units])
        run(['systemctl', 'daemon-reload'])
        run(['systemctl', 'start', 'workbuddy-autoclaim-verify.service'])
        return {'stage': 'INSTALLED_TIMER_NOT_ENABLED', 'verification': json.loads((paths[2] / 'verification-latest.json').read_text())}
    except Exception:
        cleaned = rollback(created_paths, created_units, created_user)
        return {'stage': 'STAGING_FAILED_NO_TIMER_ENABLED', 'rollback_complete': cleaned}


def main():
    raw = sys.stdin.buffer.read(4*1024*1024+1)
    assert len(raw) <= 4*1024*1024
    result = install(json.loads(raw))
    print(json.dumps(result))
    return 0 if result.get('stage') == 'INSTALLED_TIMER_NOT_ENABLED' else 1

if __name__ == '__main__':
    try: sys.exit(main())
    except Exception:
        print(json.dumps({'status': 'STAGING_FAILED_NO_TIMER_ENABLED'}))
        sys.exit(1)
