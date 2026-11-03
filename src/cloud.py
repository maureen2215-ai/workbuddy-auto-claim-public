#!/usr/bin/env python3
"""Cloud adapter; credentials are provided by systemd, never by desktop access."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import signal
import time
import fcntl
import background as b

DATA = Path('/var/lib/workbuddy-auto-claim')
CONFIG = Path('/etc/workbuddy-auto-claim/authorization.json')
HEADER_NAMES = {'Authorization', 'X-User-Id', 'X-Enterprise-Id', 'X-Tenant-Id',
                'X-Domain', 'User-Agent', 'Accept', 'Content-Type'}


def credential():
    directory = os.environ.get('CREDENTIALS_DIRECTORY')
    if not directory:
        raise b.Stop('CLOUD_CREDENTIAL_MISSING')
    value = b.load_json(Path(directory) / 'session.json')
    headers = value.get('headers')
    if (not isinstance(headers, dict) or not headers.keys() <= HEADER_NAMES or
            not {'Authorization', 'X-User-Id'} <= headers.keys()):
        raise b.Stop('CLOUD_CREDENTIAL_INVALID')
    for key, val in headers.items():
        if key == 'Authorization':
            valid = isinstance(val, str) and re.fullmatch(r'Bearer [A-Za-z0-9._~+/-]{1,32768}=*', val)
        else:
            valid = isinstance(val, str) and re.fullmatch(r'[\x20-\x7e]{1,2048}', val)
        if not valid:
            raise b.Stop('CLOUD_CREDENTIAL_INVALID')
    account = hashlib.sha256((headers['X-User-Id'] + '|' + headers.get('X-Enterprise-Id', '')
                              + '|' + headers.get('X-Domain', '')).encode()).hexdigest()[:24]
    if (value.get('account') != account or
            headers.get('X-Tenant-Id', '') != headers.get('X-Enterprise-Id', '')):
        raise b.Stop('ACCOUNT_CHANGED_REVIEW_REQUIRED')
    return headers, account


def emit(rows, mode, store=None):
    item = {'time': b.now().isoformat(), 'source': 'cloud', 'mode': mode,
            'results': rows, 'needs_attention': any(r['status'] not in b.GOOD for r in rows),
            'daily_confirmed': store.data.get('daily_confirmed') if store else None,
            'notification': 'LOCAL_MONITOR_REQUIRED'}
    # Fixed program statuses/numeric evidence only; never API bodies or exceptions.
    b.atomic(DATA / 'background-latest.json', item)
    target = DATA / 'background-events.jsonl'
    if target.exists() and target.stat().st_size > 2 * 1024 * 1024:
        os.replace(target, DATA / 'background-events.previous.jsonl')
    with target.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(item, ensure_ascii=False) + '\n')
    print(json.dumps(item, ensure_ascii=False))
    return 1 if item['needs_attention'] else 0


def execute(mode):
    store = None
    try:
        config = b.load_json(CONFIG)
        if (config.get('use_cloud_session') is not True or config.get('daily') is not True
                or config.get('endpoint') != b.BASE):
            raise b.Stop('AUTHORIZATION_REQUIRED')
        headers, account = credential()
        if config.get('expected_account') != account:
            raise b.Stop('ACCOUNT_CHANGED_REVIEW_REQUIRED')
        store = b.Store(DATA / 'background-state.json', account)
        epoch = time.time()
        daily = store.data.get('daily_confirmed') != b.now().date().isoformat()
        growth = epoch >= store.data.get('next_growth', 0)
        if mode == 'scheduled':
            if not b.due(b.now()) or epoch < store.data.get('retry_after', 0) or not (daily or growth):
                b.atomic(DATA / 'scheduler-heartbeat.json', {'time': b.now().isoformat(),
                    'source': 'cloud', 'status': 'NO_WORK_DUE',
                    'daily_confirmed': store.data.get('daily_confirmed')})
                return 0
        api = b.API(headers, writes=mode == 'scheduled')
        if mode == 'verify':
            value = api.call('POST', b.STATUS)
            checked, active = b.daily_status(value)
            # Verify mode never changes the reward ledger or sends claim requests.
            result = {'time': b.now().isoformat(), 'source': 'cloud', 'mode': 'verify',
                      'today_checked_in': checked, 'active': active,
                      'today_credit': b.safe_number(value.get('today_credit'))}
            b.atomic(DATA / 'verification-latest.json', result)
            print(json.dumps(result, ensure_ascii=False))
            return 0
        rows = b.Rewards(api, store, config).run(daily, growth)
        if growth:
            store.data['next_growth'] = epoch + 1800
        errors = [row for row in rows if row['status'] not in b.GOOD]
        store.data['retry_after'] = epoch + (3600 if any(r['status'] == 'LOGIN_REJECTED' for r in errors) else 300) if errors else 0
        store.save()
        return emit(rows, mode, store)
    except b.Stop as error:
        return emit([{'status': error.code}], mode, store)
    except Exception:
        return emit([{'status': 'CLOUD_UNEXPECTED_ERROR'}], mode, store)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=('verify', 'scheduled'))
    args = parser.parse_args()
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    DATA.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (DATA / 'run.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        return execute(args.mode)


if __name__ == '__main__':
    # The unit also enforces a timeout. Termination after a request leaves the
    # durable pending intent intact so a later run cannot blindly resubmit.
    signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(b.Stop('TIME_BUDGET_EXHAUSTED')))
    signal.alarm(175)
    raise SystemExit(main())
