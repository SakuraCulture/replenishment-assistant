"""Real local Node protocol + Qt; no browser binding or live platform."""
import json
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import closing

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tools')]
from prepare_collection_maintenance import seed
from aoxiang_ui_common import component_root
from aoxiang_client import ProcessClient
from aoxiang_jobs import Jobs
from aoxiang_acquisition import prepare,manifest


class ReexportProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.out=Path(self.temp.name)/'case';self.fixture=seed(self.out)
        # Explicit synthetic uncertain native attempt, before opening the component.
        with closing(sqlite3.connect(Path(self.fixture['state_root'])/'state.sqlite')) as db,db:
            raw=db.execute("SELECT payload FROM attempts WHERE id='attempt-W2'").fetchone()[0]
            a=json.loads(raw);a['stage']='SUBMISSION_UNKNOWN';a['errorCode']='UI_ACTION_TIMEOUT'
            db.execute('UPDATE attempts SET stage=?,payload=? WHERE id=?',(a['stage'],json.dumps(a,ensure_ascii=False,separators=(',',':')),'attempt-W2'))
        self.client=ProcessClient(component_root(),Path(self.fixture['state_root']));self.client.start()
        self.jobs=Jobs(self.out/'settings/aoxiang/jobs.sqlite3');self.config=self.client.request('configuration')
        self.addCleanup(self.close_client)

    def close_client(self):
        self.assertTrue(self.client.close())

    def test_unknown_selected_can_reexport_with_idempotent_receipt_and_original_files_unchanged(self):
        original=self.jobs.get('adopted');adoption=manifest(original,self.fixture['state_root'])
        before=(self.out/'business-before.sql').read_text('utf-8');old_other=self.jobs.get('old-failed')['record']['items'][1]
        plan=self.jobs.reexport_preview(self.client,self.config,['W2'])
        receipt=self.jobs.reexport_apply(self.client,plan);self.assertEqual(self.jobs.reexport_apply(self.client,plan),receipt)
        self.assertEqual(self.jobs.get('old-failed')['record']['items'][0]['state'],'ENDED')
        self.assertEqual(self.jobs.get('old-failed')['record']['items'][1],old_other)
        new=prepare(self.jobs,self.config,['W2'],'refresh',day='2026-11-11');self.jobs.dispatch(self.client,new['id'])
        until=time.monotonic()+6
        while time.monotonic()<until:
            state=self.jobs.refresh(self.client,new['id'])
            if state['state']=='INTERRUPTED':break
            time.sleep(.03)
        self.assertEqual(state['record']['error']['code'],'SETUP_REQUIRED')
        self.assertEqual(manifest(original,self.fixture['state_root']),adoption)
        with closing(sqlite3.connect(self.fixture['business_db'])) as db:self.assertEqual('\n'.join(db.iterdump()),before)
        self.assertTrue(self.client.close());self.client=ProcessClient(component_root(),Path(self.fixture['state_root']));self.client.start();self.jobs.synchronize(self.client)
        self.assertEqual(self.jobs.get('old-failed')['record']['items'][0]['state'],'ENDED')

    def test_reexport_does_not_retire_analysis_request(self):
        analysis=prepare(self.jobs,self.config,['W2'],'refresh',day='2026-11-11',purpose='stockout_analysis',owner='analysis-test')
        p=self.jobs.reexport_preview(self.client,self.config,['W2']);self.jobs.reexport_apply(self.client,p)
        self.assertEqual(self.jobs.get(analysis['id']),analysis)
        self.assertEqual(next(x for x in self.jobs.unfinished() if x['id']==analysis['id'])['purpose'],'stockout_analysis')

    def test_unaccepted_partial_request_keeps_other_store_and_never_dispatches_retired_code(self):
        self.jobs.reexport_apply(self.client,self.jobs.reexport_preview(self.client,self.config,['W2']))
        local=prepare(self.jobs,self.config,['W3','W2'],'refresh',day='2026-11-11')
        p=self.jobs.reexport_preview(self.client,self.config,['W3']);self.jobs.reexport_apply(self.client,p)
        saved=self.jobs.get(local['id']);self.assertEqual(saved['state'],'PREPARED');self.assertEqual(saved['context']['local_ended_codes'],['W3'])
        self.assertEqual(next(x for x in self.jobs.unfinished() if x['id']==local['id'])['command']['scope']['warehouse_codes'],['W2'])
        continued=self.jobs.dispatch(self.client,local['id'])
        self.assertNotEqual(continued['id'],local['id']);self.assertEqual(continued['command']['scope']['warehouse_codes'],['W2'])
        self.assertEqual(self.jobs.get(local['id'])['state'],'CANCELLED')


from tests import test_collection_maintenance as maintenance


class ReexportUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app=QApplication.instance() or QApplication([])
    setUp=maintenance.MaintenanceUiTests.setUp
    tearDown=maintenance.MaintenanceUiTests.tearDown
    wait=maintenance.MaintenanceUiTests.wait
    def test_reexport_button_cancel_then_selected_scope(self):
        d=self.d
        for row,check in enumerate(d.checks):check.setChecked(d.stores.item(row,2).text()=='W2')
        before=d.jobs.get('old-failed')['record']['items'][1]
        with patch.object(d,'confirm_maintenance',return_value=False):
            d.reexport_btn.click();self.wait(lambda:d.worker is None)
        self.assertEqual(d.jobs.get('blocked')['state'],'NEEDS_REVIEW')
        with patch.object(d,'confirm_maintenance',return_value=True):
            d.reexport_btn.click();self.wait(lambda:d.worker is None and d.current['id'].startswith('rep-'))
        self.assertEqual(d.current['command']['scope']['warehouse_codes'],['W2'])
        self.assertEqual(d.jobs.get('old-failed')['record']['items'][1],before)
        self.assertFalse(self.errors)


if __name__=='__main__':unittest.main()
