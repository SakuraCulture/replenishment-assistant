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
        self.errors = {}
        self.accounts = [
            {'warehouse': name, 'enabled': True, 'bindingVersion': i + 1, 'expectedCompany': '隔离' + name,
             'binding': {'actualProfilePath': 'C:/QA/' + key + '/Default'}}
            for i, (name, key) in enumerate([('山东仓', 'sd'), ('河北仓', 'hb')])
        ]
        self.rebind_required = {}
        self.binding_epochs = {}
        self.batches = []
        self.delay = 0

    def start(self):
        self.running = True; return {}

    def request(self, action, **values):
        self.calls.append((action, copy.deepcopy(values)))
        if self.delay: time.sleep(self.delay)
        if action == 'sync.status':
            return copy.deepcopy({'accounts': self.accounts, 'operations': self.operations, 'running': False,
                                  'active': None, 'rebind_required': self.rebind_required,
                                  'binding_epochs': self.binding_epochs, 'batches': self.batches})
        if action == 'accounts.ensure':
            name = values['warehouse']; ident = 'qa-' + name; error = self.errors.get(name)
            self.operations[ident] = {'state': 'LOGIN_REQUIRED' if error and error['code'] == 'LOGIN_REQUIRED' else 'FAILED' if error else 'SUCCEEDED',
                                      'error': error, 'result': {'scope': {'company': '隔离' + name}}}
            return {'operation_id': ident}
        if action == 'accounts.auto':
            name = values['warehouse']
            assert self.rebind_required.get(name) and values.get('require_confirmation') is True
            assert values['expected_version'] == self.binding_epochs[name]
            ident = f'qa-repair-{len(self.operations)}'
            self.operations[ident] = {
                'id': ident, 'warehouse': name, 'state': 'SUCCEEDED', 'bindingEpoch': self.binding_epochs[name],
                'result': {'scope': {'company': '隔离' + name}, 'binding': {'actualProfilePath': 'C:/QA/repair/Default'},
                           'requires_confirmation': True},
            }
            return {'operation_id': ident}
        if action == 'accounts.bind':
            name = values['warehouse']
            probe = self.operations[values['probe_id']]
            assert probe['warehouse'] == name and values['mapping_verified'] is True
            assert values['expected_version'] == self.binding_epochs[name] == probe['bindingEpoch']
            account = {'warehouse': name, 'enabled': True, 'bindingVersion': self.binding_epochs[name] + 1,
                       'expectedCompany': probe['result']['scope']['company'], 'binding': probe['result']['binding']}
            self.accounts.append(account)
            self.binding_epochs[name] = account['bindingVersion']
            self.rebind_required.pop(name)
            return copy.deepcopy(account)
        if action == 'session.health': return {'connections': [{'warehouse': n, 'state': 'CONNECTED'} for n in ['山东仓', '河北仓']]}
        if action == 'session.release': return {'idle': True}
        if action == 'sync.stop': return {'stop_requested': True, 'still_running': False}
        raise AssertionError('Unexpected JST startup action: ' + action)

    def close(self, timeout=.5):
        self.running = False; return True
