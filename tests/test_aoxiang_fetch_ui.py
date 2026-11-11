import copy
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from test_aoxiang_acquisition import configuration, complete
from aoxiang_jobs import fingerprint


class FakeClient:
    records = {}

    def __init__(self, component_root, state_root):
        self.state_root = state_root
        self.running = False
        self.calls = []
        self.finish = False
        self.jobs = None

    def start(self):
        self.running = True

    def close(self, timeout=10):
        self.running = False
        return True

    def request(self, action, **params):
        self.calls.append((action, copy.deepcopy(params)))
        if action == 'configuration':
            return configuration()
        if action == 'events':
            return {'events': []}
        if action == 'reserve':
            command = params['command']
            record = {'id': command['command_id'], 'request': command, 'fingerprint': fingerprint(command),
                      'sequence': 1, 'state': 'QUEUED', 'items': [
                          {'index': index, 'warehouse_code': code, 'run_id': f'run-{index}', 'state': 'PENDING', 'result': None, 'error': None}
                          for index, code in enumerate(command['scope']['warehouse_codes'])]}
            self.records.setdefault(record['id'], record)
            return {'record': copy.deepcopy(self.records[record['id']]), 'created': True}
        command_id = params['command_id']
        if command_id not in self.records:
            from aoxiang_client import ComponentError
            raise ComponentError('COMMAND_NOT_FOUND','没有该本机命令')
        record = self.records[command_id]
        if action == 'pause':
            record['sequence'] += 1
            record['state'] = 'PAUSED'
        elif action in ('resume', 'collection.continue'):
            record['sequence'] += 1
            record['state'] = 'RUNNING'
        elif action == 'status' and self.finish:
            # completed fixture must use a newer receipt than pause/resume.
            job = self.jobs.get(command_id)
            values = complete_fixture(self.state_root, job)
            values['sequence'] = record['sequence'] + 1
            record.update(values)
            self.finish = False
        return copy.deepcopy(record)


def complete_fixture(root, job):
    from aoxiang_directory import file_hash
    record = {'state': 'SUCCEEDED', 'items': []}
    for index, code in enumerate(job['command']['scope']['warehouse_codes']):
        temp = root / 'temp.xlsx'
        temp.parent.mkdir(parents=True, exist_ok=True)
        temp.write_bytes(code.encode())
        digest = file_hash(temp)
        dest = root / 'command-files' / job['id'] / f'{index}-{digest}.xlsx'
        dest.parent.mkdir(parents=True, exist_ok=True)
        temp.rename(dest)
        record['items'].append({'index': index, 'warehouse_code': code, 'run_id': f'run-{index}',
            'state': 'SUCCEEDED', 'error': None, 'result': {'warehouse_code': code, 'run_id': f'run-{index}',
            'account_binding': job['command']['account_binding'], 'file': str(dest.resolve()), 'sha256': digest,
            'bytes': dest.stat().st_size, 'rows': 1, 'reused': False}})
    return record


class FetchUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from aoxiang_fetch_ui import FetchDialog
        self.temp = tempfile.TemporaryDirectory()
        self.errors = []
        self.warning = patch('readable_ui.failure', side_effect=lambda *a: self.errors.append(a[-1]))
        self.warning.start()
        FakeClient.records = {}
        self.dialog = FetchDialog(None, self.temp.name, '2026-11-02', client_factory=FakeClient)
        self.dialog.show()
        self.wait(lambda: self.dialog.worker is None)
        self.dialog.client.jobs = self.dialog.jobs

    def wait(self, check):
        from PySide6.QtTest import QTest
        until = time.monotonic() + 6
        while not check() and time.monotonic() < until:
            self.app.processEvents()
            QTest.qWait(10)
        self.assertTrue(check())

    def tearDown(self):
        import shiboken6
        self.dialog.close()
        self.wait(lambda: self.dialog._can_close and self.dialog.worker is None)
        shiboken6.delete(self.dialog)
        self.warning.stop()
        self.temp.cleanup()

    def test_selection_pause_resume_same_id_then_manifest_order(self):
        d = self.dialog
        d.search.setText('门店一')
        d.select_visible()
        self.assertEqual(d.selected_codes(), ['W1'])
        d.search.clear()
        d.select_visible()
        self.assertEqual(d.selected_codes(), ['W2', 'W1'])
        d.start_fetch()
        self.wait(lambda: d.worker is None and d.current is not None)
        command_id = d.current['id']
        self.assertFalse(d.start_btn.isEnabled())
        self.assertFalse(d.adopt_btn.isEnabled())
        d.pause_fetch()
        self.wait(lambda: d.worker is None and d.current['state'] == 'PAUSED')
        self.assertTrue(d.resume_btn.isEnabled())
        d.resume_fetch()
        self.wait(lambda: d.worker is None and d.current['state'] == 'RUNNING')
        d.client.finish = True
        d.refresh_job()
        self.wait(lambda: d.worker is None and d.current['state'] == 'SUCCEEDED')
        self.assertEqual(d.current['id'], command_id)
        self.assertEqual(len([a for a, _ in d.client.calls if a == 'reserve']), 1)
        self.assertTrue(d.adopt_btn.isEnabled())
        d.select_adoption()
        self.wait(lambda: d._can_close and d.worker is None)
        self.assertEqual([f['warehouse_code'] for f in d.adoption['files']], ['W2', 'W1'])
        self.assertFalse(self.errors)

    def test_new_start_locates_hidden_original_blocker_without_registering_another_job(self):
        from aoxiang_acquisition import prepare
        d=self.dialog;old=prepare(d.jobs,configuration(),['W1'],'refresh',day='2026-11-01')
        displayed=prepare(d.jobs,configuration(),['W2'],'refresh',day='2026-11-02')
        d.show_job(displayed)
        for row,check in enumerate(d.checks):check.setChecked(d.stores.item(row,2).text()=='W1')
        d.start_fetch();self.wait(lambda:d.worker is None)
        self.assertEqual(d.current['id'],old['id']);self.assertEqual(d.history.currentData(),old['id'])
        self.assertEqual(d.details.rowCount(),1);self.assertIn('W1',d.details.item(0,0).text());self.assertIn('受理情况尚未核对',d.details.item(0,4).text())
        self.assertEqual(d.selected_codes(),['W1']);self.assertEqual(len(d.jobs.recent()),2)
        self.assertTrue(any(old['id'] in e and '2026-11-01' in e and '门店一' in e for e in self.errors))
        self.assertFalse(any(action=='reserve' for action,_ in d.client.calls));self.assertGreater(d.progress.maximum(),0)

    def test_new_login_failure_stops_indeterminate_progress_and_preserves_selected_stores(self):
        from aoxiang_client import ComponentError
        d=self.dialog;d.select_visible()
        with patch('aoxiang_current_login.current_configuration',side_effect=ComponentError('IDENTITY_RESPONSE_TIMEOUT','本次身份查询未完成')):
            d.start_fetch();self.assertEqual(d.progress.maximum(),0);self.wait(lambda:d.worker is None)
        self.assertEqual(d.progress.maximum(),1);self.assertEqual(d.progress.value(),0);self.assertFalse(d._checking_new_fetch)
        self.assertEqual(d.selected_codes(),['W2','W1']);self.assertTrue(d.start_btn.isEnabled());self.assertEqual(d.jobs.recent(),[])
        self.assertTrue(any('本次身份查询未完成' in e for e in self.errors))

    def test_review_uses_dedicated_continue_without_replaying_unknown_item(self):
        d = self.dialog
        d.select_visible()
        d.start_fetch()
        self.wait(lambda: d.worker is None and d.current is not None)
        job = copy.deepcopy(d.current)
        job['state'] = job['record']['state'] = 'NEEDS_REVIEW'
        job['record']['items'][0]['state'] = 'NEEDS_REVIEW'
        job['record']['sequence'] += 1
        d.client.records[job['id']] = copy.deepcopy(job['record'])
        d.jobs.observe(job['id'],job['record'])
        job=d.jobs.get(job['id'])
        d.show_job(job)
        self.assertTrue(d.resume_btn.isEnabled())
        self.assertFalse(d.adopt_btn.isEnabled())
        self.assertFalse(d.poll.isActive())
        d.resume_btn.click()
        self.wait(lambda:d.worker is None and d.current['state']=='RUNNING')
        self.assertEqual(d.current['record']['items'][0]['state'],'NEEDS_REVIEW')
        self.assertEqual(len([a for a,_ in d.client.calls if a=='collection.continue']),1)
        self.assertFalse(any(a in ('resume','retry') for a,_ in d.client.calls))
        self.assertEqual(len([a for a,_ in d.client.calls if a=='reserve']),1)


class AdoptionUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from application_window import ApplicationWindow
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, REPLENISHMENT_DATA_DIR=self.temp.name)
        self.env.start()
        with patch('history_tasks.HistoryReader.request'):
            self.host = ApplicationWindow()
        self.host.master_legacy.setChecked(True)
        self.host.products.path = str(Path(self.temp.name) / 'master.xlsx')
        self.host.auto_check.stop()
        self.value = {'files': [{'file': str(Path(self.temp.name) / 'sales.xlsx'), 'sha256': 'abc', 'warehouse_code': 'W2'}]}
        self.host.results = {'W2': {'sentinel': [1]}}
        self.host.preissue_drafts = {'W2': {'edits': {'SKU': {'quantity': 24}}}}
        self.host.job_dirty = True

    def tearDown(self):
        import shiboken6
        self.host.results = {}
        self.host.preissue_drafts = {}
        self.host.job_dirty = False
        self.host.close()
        self.app.processEvents()
        shiboken6.delete(self.host)
        self.env.stop()
        self.temp.cleanup()

    def test_cancel_before_adoption_keeps_state_and_does_not_start_check(self):
        from aoxiang_input_ui import adopt
        from PySide6.QtWidgets import QMessageBox
        before = copy.deepcopy((self.host.results, self.host.preissue_drafts, self.host.sales.paths))
        with patch('aoxiang_input_ui.QMessageBox.question', return_value=QMessageBox.StandardButton.No), patch.object(self.host, 'job') as job:
            self.assertFalse(adopt(self.host, self.value))
            job.assert_not_called()
        self.assertEqual((self.host.results, self.host.preissue_drafts, self.host.sales.paths), before)

    def test_check_uses_original_worker_and_only_adopts_once_after_success(self):
        from aoxiang_input_ui import adopt
        import batch_service
        from PySide6.QtWidgets import QMessageBox
        h = self.host
        callbacks = []
        def run(fn, args, callback):
            self.assertIs(fn, batch_service.load)
            self.assertEqual(args, ([self.value['files'][0]['file']], h.products.path))
            callbacks.append(callback)
        with patch('aoxiang_input_ui.QMessageBox.question', return_value=QMessageBox.StandardButton.Yes), patch.object(h, 'leave_current', return_value=True), patch.object(h, 'job', side_effect=run):
            self.assertTrue(adopt(h, self.value))
        self.assertEqual(h.preissue_drafts['W2']['edits']['SKU']['quantity'], 24)
        loaded = {'intake': {'files': [{'path': self.value['files'][0]['file'], 'error': None}],
                             'hashes': {self.value['files'][0]['file']: 'abc'}}}
        with patch.object(h, 'loaded_ok') as adopted, patch.object(h, 'check_added_files') as auto:
            callbacks[0](loaded)
            adopted.assert_called_once_with(loaded)
            self.assertEqual(h.sales.paths, [self.value['files'][0]['file']])
            auto.assert_not_called()
            self.assertFalse(h.auto_check.isActive())

    def test_mismatch_after_check_keeps_original_results(self):
        from aoxiang_input_ui import adopt
        from PySide6.QtWidgets import QMessageBox
        h = self.host
        callbacks = []
        before = copy.deepcopy((h.results, h.preissue_drafts, h.sales.paths))
        with patch('aoxiang_input_ui.QMessageBox.question', return_value=QMessageBox.StandardButton.Yes), patch.object(h, 'leave_current', return_value=True), patch.object(h, 'job', side_effect=lambda fn, args, cb: callbacks.append(cb)):
            adopt(h, self.value)
        loaded = {'intake': {'files': [{'path': self.value['files'][0]['file'], 'error': None}], 'hashes': {}}}
        with patch.object(h, 'failed') as failure, patch.object(h, 'loaded_ok') as adopted:
            callbacks[0](loaded)
            failure.assert_called_once()
            adopted.assert_not_called()
        self.assertEqual((h.results, h.preissue_drafts, h.sales.paths), before)


if __name__ == '__main__':
    unittest.main()
