"""Opt-in local EXE check with an explicitly simulated read-only transport."""
from copy import deepcopy
import readable_ui
from PySide6.QtTest import QTest
from aoxiang_setup_ui import SetupDialog
from aoxiang_client import ComponentError


def exercise_setup(win, output, wait, active, checks):
    class SimulatedClient:
        instances = 0

        def __init__(self, *args):
            type(self).instances += 1
            self.fail_status = type(self).instances == 1
            self.running = False
            self.calls = []

        def start(self):
            self.running = True
            return {'configured': False}

        def request(self, action, **values):
            self.calls.append(action)
            if action == 'discover':
                return {'installations': [{'executable': 'C:/QA/edge.exe', 'version': '模拟，不连接平台'}],
                        'profiles': [{'displayName': '隔离验收', 'profileDirectory': 'QA', 'actualProfilePath': 'C:/QA/profile'}]}
            if action == 'configuration':
                return {'configured': False}
            if action == 'setup.start':
                self.job = {'id': 'offline', 'state': 'RUNNING', 'stage': '只读核验模拟', 'current': 0,
                            'total': len(values['targets']), 'error': None, 'targets': []}
            elif action == 'setup.status':
                if self.fail_status:
                    raise ComponentError('RESPONSE_UNKNOWN', '隔离模拟：组件状态应答超时')
                self.job.update(state='READY', stage='待保存', current=self.job['total'], verified=self.job['total'], failed=0)
            else:
                raise ValueError('EXE smoke forbids platform/write operations: '+action)
            return deepcopy(self.job)

        def close(self, timeout=10):
            self.running = False
            return True

    warnings = []
    original = readable_ui.failure
    def expected_failure(parent,title,error):
        if '隔离模拟：组件状态应答超时' not in str(error):
            return original(parent,title,error)
        warnings.append(str(error))
    readable_ui.failure = expected_failure
    dialog = SetupDialog(win, output/'setup-simulated', client_factory=SimulatedClient)
    active['dialog'] = dialog
    try:
        dialog.show()
        wait(lambda:dialog.worker is None)
        dialog.data = {'items': [{'code': 'W2', 'name': '本地模拟门店2'}, {'code': 'W1', 'name': '本地模拟门店1'}]}
        dialog.fill_directory()
        dialog.detect_btn.click()
        wait(lambda:dialog.worker is None)
        if dialog.progress.value() != 0 or dialog.verify_btn.isEnabled():
            raise ValueError('Discovery incorrectly shown as completed verification')
        dialog.grab().save(str(output/'setup-detected.png'))
        dialog.all_btn.click()
        dialog.verify_btn.click()
        wait(lambda:dialog.transport_failed and dialog.worker is None)
        QTest.qWait(750)
        if len(warnings) != 1 or dialog.poll.isActive() or not dialog.reconnect_btn.isEnabled() or dialog.save_btn.isEnabled():
            raise ValueError('Failed status polling not stopped safely')
        dialog.grab().save(str(output/'setup-status-failed.png'))
        old = dialog.client
        dialog.reconnect_btn.click()
        wait(lambda:dialog.client is not old and dialog.worker is None and not dialog.detect_pending)
        dialog.verify_btn.click()
        wait(lambda:dialog.setup_state == 'READY' and dialog.worker is None)
        if not dialog.save_btn.isEnabled():
            raise ValueError('Read-only retry did not recover')
        checks.append('实际EXE核验窗口：模拟检测非100%、勾选启用、状态异常单次提示与停止轮询、重新连接后可核验；未连接平台或保存配置')
    finally:
        readable_ui.failure = original
        dialog.close()
        wait(lambda:dialog._can_close and dialog.worker is None)
        dialog.deleteLater()
        active['dialog'] = None
