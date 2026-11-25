"""Launch the real Windows entry with isolated data and exercise its watchdog."""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CASES = {'normal-close': 'CLOSED', 'scheduled': 'CLOSED', 'receipt-pending': 'CLOSED',
         'disabled': 'DISABLED', 'crash-loop': 'LIMIT_REACHED',
         'stockout-queued': 'CLOSED','stockout-claimed': 'CLOSED'}


def read(path):
    return json.loads(path.read_text('utf-8'))


def verify(mode, out, command):
    out.mkdir()
    (out / 'synthetic.json').write_text(json.dumps({'synthetic': True, 'live_platform': False, 'mode': mode}), 'utf-8')
    env = {**os.environ, 'REPLENISHMENT_DATA_DIR': str(out / 'settings'),
           'LOCALAPPDATA': str(out / 'localappdata'), 'PYTHONUTF8': '1'}
    for name in ('PYTHONHOME', 'QT_PLUGIN_PATH', 'QT_QPA_PLATFORM', 'REPLENISHMENT_APP_RECOVERY_TICKET'):
        env.pop(name, None)
    with (out / 'process.log').open('w', encoding='utf-8') as log:
        process = subprocess.Popen(command + ['--app-recovery-smoke', str(out)], env=env, stdout=log, stderr=log)
        started = time.monotonic()
        while time.monotonic() - started < 120:
            if (out / 'started.json').is_file():
                owner = read(out / 'started.json')
                state_path = Path(owner['status_path'])
                if state_path.is_file():
                    state = read(state_path)
                    if state['state'] in ('CLOSED', 'DISABLED', 'LIMIT_REACHED', 'WATCH_FAILED', 'DATA_UNAVAILABLE', 'SUPERSEDED'):
                        break
            time.sleep(.1)
        else:
            raise TimeoutError(f'验收未完成 {out}，初始进程退出状态 {process.poll()}')
    assert state['state'] == CASES[mode], state
    assert state['watchdog_pid'] != owner['pid'], state
    code = process.wait(timeout=10)
    assert code == (0 if mode == 'normal-close' else 23), code
    attempts = [event for event in state['events'] if event['state'] == 'RESTARTING']
    assert len(attempts) == {'normal-close': 0, 'disabled': 0, 'crash-loop': 3}.get(mode,1), state
    result = {'mode': mode, 'original_exit_code': code, 'watchdog_pid': state['watchdog_pid'],
              'original_pid': owner['pid'], 'restart_attempts': len(attempts), 'watchdog_final_state': state['state'],
              'elapsed_seconds': round(time.monotonic() - started, 2), 'watchdog_log': str(state_path)}
    if state['state'] == 'CLOSED':
        report = read(out / 'report.json')
        assert report['passed'], report
        if mode != 'normal-close':
            assert report['original_main_pid'] == owner['pid'] != report['pid']
            if mode.startswith('stockout-'):
                assert report['production_platform_submissions']==0 and report['original_run_id']
            else:
                assert report['completed'] == [1, 1 if mode == 'receipt-pending' else 2] and report['reserve_calls'] == 1
        result['desktop'] = report
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--exe', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--case', choices=CASES, action='append')
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    command = [str(args.exe.resolve())] if args.exe else [sys.executable, str(ROOT / 'src/app.py')]
    results = [verify(case, out / case, command) for case in (args.case or CASES)]
    exe_hash = None
    if args.exe:
        with args.exe.open('rb') as file:
            exe_hash = hashlib.file_digest(file, 'sha256').hexdigest()
    report = {'passed': True, 'exe_sha256': exe_hash, 'live_platform': False, 'cases': results}
    (out / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), 'utf-8')
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
