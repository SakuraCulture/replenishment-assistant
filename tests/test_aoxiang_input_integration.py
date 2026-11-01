"""Real check worker, two Excel files, original retention, calculation and export comparison."""
import copy
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tools')]
from aoxiang_acquisition import prepare, manifest
from aoxiang_jobs import Jobs
from aoxiang_directory import file_hash
from test_aoxiang_acquisition import configuration
from parallel_qa import diff, clean
from structure_oracle import excel_snapshot


class ActualInputTests(unittest.TestCase):
    def test_automatic_manual_source_rows_results_and_workbooks_equal(self):
        import openpyxl
        import engine
        import batch_service
        import parameter_schema
        from application_window import ApplicationWindow
        from aoxiang_input_ui import adopt
        from PySide6.QtWidgets import QApplication
        from PySide6.QtTest import QTest
        import shiboken6
        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            jobs = Jobs(root / 'ax/jobs.sqlite3')
            job = prepare(jobs, configuration(), ['W2', 'W1'], 'reuse', day='2026-11-02')
            items = []
            for index, code in enumerate(['W2', 'W1']):
                file = root / f'original-{code}.xlsx'
                wb = openpyxl.Workbook()
                ws = wb.active
                ws.title = '商品补货建议'
                ws.append(['仓库/门店编码', '仓库/门店名称', '商品编码', '商品名称', '日销', '周销', '月销', '可用库存', '在途库存', '缺货天数（天）'])
                ws.append([code, '测试店' + code, '0001', '测试饮料', 1, 7, 30, index, 0, 0])
                ws.append([code, '测试店' + code, '0002', '测试箱规', 2, 14, 60, 0, 0, 0])
                wb.save(file)
                wb.close()
                digest = file_hash(file)
                dest = root / 'ax/command-files' / job['id'] / f'{index}-{digest}.xlsx'
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(file.read_bytes())
                result = {'warehouse_code': code, 'run_id': f'run-{index}', 'file': str(dest.resolve()),
                          'sha256': digest, 'bytes': dest.stat().st_size, 'rows': 2,
                          'account_binding': configuration()['account_binding'], 'reused': False}
                items.append({'index': index, 'warehouse_code': code, 'run_id': f'run-{index}', 'state': 'SUCCEEDED', 'result': result, 'error': None})
            jobs.observe(job['id'], {'id': job['id'], 'fingerprint': job['fingerprint'], 'request': job['command'], 'state': 'SUCCEEDED', 'sequence': 2, 'items': items})
            value = manifest(jobs.get(job['id']), root / 'ax')
            master = root / '商品资料.xlsx'
            wb = openpyxl.Workbook()
            wb.active.append(['商品编码', '商品名称', '分类', '基本售价', '出库箱规', '商品状态', '仓归属'])
            wb.active.append(['0001', '测试饮料', '饮料', 2, 1, '启用', '河北仓'])
            wb.active.append(['0002', '测试箱规', '饮料', 3, 6, '启用', '河北仓'])
            wb.save(master)
            wb.close()
            paths = [item['file'] for item in value['files']]
            manual = batch_service.load(paths, str(master))
            with patch.dict(os.environ, REPLENISHMENT_DATA_DIR=str(root / 'settings')):
                with patch('history_tasks.HistoryReader.request'):
                    host = ApplicationWindow()
            errors = []
            try:
                host.master_legacy.setChecked(True)
                host.products.path = str(master)
                host.auto_check.stop()
                host.job_dirty = False
                with patch.object(host, 'failed', side_effect=errors.append):
                    self.assertTrue(adopt(host, value))
                    until = time.monotonic() + 30
                    while host.worker is not None and time.monotonic() < until:
                        app.processEvents()
                        QTest.qWait(10)
                self.assertIsNone(host.worker)
                self.assertFalse(errors)
                self.assertEqual(host.sales.paths, paths)
                self.assertEqual(list(host.loaded['stores']), ['W2', 'W1'])
                self.assertEqual(diff(clean(manual), clean(host.loaded)), [])
                self.assertTrue(all(w['mode'].currentIndex() == 0 for w in host.store_widgets.values()), 'Missing budgets must not become unlimited')
                with closing(sqlite3.connect(host.work_repository.path)) as db:
                    retained = dict(db.execute('SELECT sha,content FROM source_files').fetchall())
                    for file in [*paths, str(master)]:
                        self.assertEqual(retained[file_hash(file)], Path(file).read_bytes())
                profile = json.loads((ROOT / 'src/default_must_sell.json').read_text(encoding='utf-8'))
                for code, budget in [('W2', 30), ('W1', None)]:
                    settings = {'warehouses': engine.DEFAULT_WAREHOUSES, 'exception': True, 'budget': budget, 'parameter_config': parameter_schema.standard()}
                    a = batch_service.calculate_store(manual, code, settings, profile)
                    b = batch_service.calculate_store(host.loaded, code, settings, profile)
                    self.assertEqual(diff(clean(a), clean(b)), [])
                    engine.export_xlsx(a, root / (code + '-manual.xlsx'))
                    engine.export_xlsx(b, root / (code + '-auto.xlsx'))
                    self.assertEqual(diff(excel_snapshot(root / (code + '-manual.xlsx')), excel_snapshot(root / (code + '-auto.xlsx'))), [])
            finally:
                if host.worker:
                    host.cancel_work()
                    while host.worker:
                        app.processEvents()
                        QTest.qWait(10)
                host.job_dirty = False
                host.close()
                app.processEvents()
                shiboken6.delete(host)


if __name__ == '__main__':
    unittest.main()
