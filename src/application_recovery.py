"""Supervise an exited desktop process from a separate, per-session process."""
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

import data_location
from stockout_common import atomic_json, now, read_json

WATCH_FLAG = '--app-recovery-watch'
SMOKE_FLAG = '--app-recovery-smoke'
TICKET_ENV = 'REPLENISHMENT_APP_RECOVERY_TICKET'
RESTART_DELAYS = (2, 8, 20)
RESTART_WINDOW = 600


def _read(path):
    try:
        value = read_json(path, 65536)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _folder(root):
    if os.environ.get('REPLENISHMENT_DATA_DIR'):
        return root.parent / ('.' + root.name + '.diagnostics') / 'process-recovery'
    return data_location.control() / 'process-recovery'


def _arguments(args, root):
    if not args:
        return []
    if len(args) == 2 and args[0] == SMOKE_FLAG and os.environ.get('REPLENISHMENT_DATA_DIR'):
        out = Path(args[1]).resolve()
        if root == out / 'settings' and _read(out / 'synthetic.json').get('synthetic') is True:
            return [SMOKE_FLAG, str(out)]
    return None


def _spawn(args, *, ticket=None):
    command = [sys.executable]
    if not getattr(sys, 'frozen', False):
        command.append(str(Path(__file__).with_name('app.py')))
    env = dict(os.environ)
    env.pop(TICKET_ENV, None)
    if ticket:
        env[TICKET_ENV] = ticket
    if getattr(sys, 'frozen', False):
        env['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
    cwd = Path(command[-1]).parent
    return subprocess.Popen(command + args, cwd=cwd, env=env, creationflags=subprocess.CREATE_NO_WINDOW)


def _enabled(folder):
    path = folder / 'preferences.json'
    return not path.exists() or _read(path).get('enabled') is True


class Session:
    def __init__(self, folder, owner):
        self.folder = folder
        self.owner = owner
        self.restarted = owner['attempt'] > 0
        self.monitor = None

    @classmethod
    def prepare(cls, root, argv):
        ticket = os.environ.pop(TICKET_ENV, None)
        if os.name != 'nt':
            return None
        root = Path(root).resolve()
        args = _arguments(argv[1:], root)
        if args is None:
            if ticket:
                raise ValueError('自动重启的启动入口无效')
            return None
        from application_processes import process_creation
        folder = _folder(root)
        session, attempt = uuid.uuid4().hex, 0
        if ticket:
            prior = _read(folder / 'owner.json')
            session = prior.get('session', '')
            if not isinstance(session, str) or not re.fullmatch('[0-9a-f]{32}', session):
                raise ValueError('找不到原自动重启记录')
            change = _read(folder / (session + '.json'))
            attempt = change.get('attempt')
            if (prior.get('root') != str(root) or prior.get('args') != args or prior.get('expected_exit')
                    or change.get('state') != 'RESTARTING' or change.get('ticket') != ticket
                    or type(attempt) is not int or type(prior.get('attempt')) is not int
                    or attempt <= prior['attempt'] or not _enabled(folder)):
                raise ValueError('原资料位置或自动重启凭据已变化')
        owner = {'session': session, 'attempt': attempt, 'pid': os.getpid(), 'created': process_creation(),
                 'root': str(root), 'args': args, 'expected_exit': False}
        atomic_json(folder / 'owner.json', owner)
        return cls(folder, owner)

    def enabled(self):
        return _enabled(self.folder)

    def start(self):
        if self.restarted or not self.enabled() or self.monitor and self.monitor.poll() is None:
            return
        self.monitor = _spawn([WATCH_FLAG, self.owner['session']])

    def set_enabled(self, enabled):
        atomic_json(self.folder / 'preferences.json', {'enabled': bool(enabled)})
        if enabled:
            self.start()

    def expect_exit(self):
        if not self.owner['expected_exit']:
            expected = {**self.owner, 'expected_exit': True}
            atomic_json(self.folder / 'owner.json', expected)
            self.owner = expected


def watch(args):
    # This helper only reads machine-local identity/settings and writes its own log.
    if os.name != 'nt' or len(args) != 1 or not re.fullmatch('[0-9a-f]{32}', args[0]):
        return 2
    from application_processes import wait_for_owned_exit
    session, = args
    folder = _folder(data_location.hint().resolve())
    first = _read(folder / 'owner.json')
    if first.get('session') != session or first.get('expected_exit'):
        return 0
    root = Path(first['root'])
    argv = _arguments(first.get('args'), root) if isinstance(first.get('args'), list) else None
    if argv is None or first.get('attempt') != 0:
        return 2
    status_path = folder / (session + '.json')
    events, attempts = [], []
    attempt = 0

    def record(state, *, ticket=None, **details):
        events.append({'at': now(), 'state': state, 'attempt': attempt, **details})
        current = {**events[-1], 'watchdog_pid': os.getpid(), 'events': events[-24:]}
        if ticket is not None:
            current['ticket'] = ticket
        atomic_json(status_path, current)

    try:
        exit_code = wait_for_owned_exit(first['pid'], first['created'],
                                         lambda: record('WATCHING', main_pid=first['pid']))
        while True:
            prior = _read(folder / 'owner.json')
            if prior.get('session') != session:
                record('SUPERSEDED')
                return 0
            if prior.get('expected_exit') or exit_code == 0:
                record('CLOSED', exit_code=exit_code)
                return 0
            attempts = [stamp for stamp in attempts if time.monotonic() - stamp < RESTART_WINDOW]
            if len(attempts) >= len(RESTART_DELAYS):
                record('LIMIT_REACHED', exit_code=exit_code)
                return 0
            delay = RESTART_DELAYS[len(attempts)]
            record('BACKOFF', exit_code=exit_code, delay=delay)
            time.sleep(delay)
            current = _read(folder / 'owner.json')
            if current != prior:
                record('SUPERSEDED')
                return 0
            if not _enabled(folder):
                record('DISABLED')
                return 0
            if data_location.root().resolve() != root or not (root / 'workbench.sqlite3').is_file():
                record('DATA_UNAVAILABLE')
                return 0
            attempt += 1
            attempts.append(time.monotonic())
            ticket = uuid.uuid4().hex
            record('RESTARTING', ticket=ticket, previous_pid=prior['pid'])
            try:
                child = _spawn(argv, ticket=ticket)
            except OSError as exc:
                record('LAUNCH_FAILED', error=str(exc))
                exit_code = None
                continue
            exit_code = child.wait()
    except (OSError, ValueError, KeyError) as exc:
        record('WATCH_FAILED', error=str(exc))
        return 1
