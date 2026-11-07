"""Closed fake transports for opt-in startup acceptance; unexpected actions fail."""
import copy
import threading
import time


class AxFixture:
    def __init__(self, *args):
        self.running = False; self.calls = []; self.error = None; self._failure = None
        self.login_error = None; self.gate = None; self.job = None; self.stopped = False

    def start(self):
        self.running = True
        if self.gate: self.gate.wait(5)
        return {'configured': True}

    def request(self, action, **values):
        self.calls.append((action, copy.deepcopy(values)))
        if action == 'configuration': return {'configured': True, 'enabled': True}
        if action == 'setup.check_login':
            self.job = {'id': 'qa-login', 'state': 'FAILED' if self.login_error else 'VERIFIED',
                        'identity': {'display_name': '隔离商家'}, 'error': self.login_error}
            return copy.deepcopy(self.job)
        if action == 'setup.status': return copy.deepcopy(self.job)
        if action == 'setup.cancel': self.stopped = True; return {'state': 'CANCELLED'}
        if action == 'session.health': return {'state': 'CONNECTED'}
        if action == 'session.release': return {'idle': True}
        raise AssertionError('Unexpected AX startup action: ' + action)

    def close(self, timeout=.5):
        self.running = False; return True


class JstFixture:
    def __init__(self, *args):
        self.running = False; self.calls = []; self.error = None; self.operations = {}
        self.errors = {}; self.accounts = [{'warehouse': name, 'enabled': True, 'bindingVersion': i + 1}
                                          for i, name in enumerate(['山东仓', '河北仓'])]
        self.delay = 0

    def start(self):
        self.running = True; return {}

    def request(self, action, **values):
        self.calls.append((action, copy.deepcopy(values)))
        if self.delay: time.sleep(self.delay)
        if action == 'sync.status': return {'accounts': copy.deepcopy(self.accounts), 'operations': copy.deepcopy(self.operations), 'running': False, 'active': None}
        if action == 'accounts.ensure':
            name = values['warehouse']; ident = 'qa-' + name; error = self.errors.get(name)
            self.operations[ident] = {'state': 'LOGIN_REQUIRED' if error and error['code'] == 'LOGIN_REQUIRED' else 'FAILED' if error else 'SUCCEEDED',
                                      'error': error, 'result': {'scope': {'company': '隔离' + name}}}
            return {'operation_id': ident}
        if action == 'session.health': return {'connections': [{'warehouse': n, 'state': 'CONNECTED'} for n in ['山东仓', '河北仓']]}
        if action == 'session.release': return {'idle': True}
        if action == 'sync.stop': return {'stop_requested': True, 'still_running': False}
        raise AssertionError('Unexpected JST startup action: ' + action)

    def close(self, timeout=.5):
        self.running = False; return True
