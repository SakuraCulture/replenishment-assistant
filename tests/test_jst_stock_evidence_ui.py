"""Native control routing for frozen stock evidence, isolated synthetic sources."""
import copy,os,sys,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import test_jst as fixtures
import jst_batches as batches,jst_results as results


class StockEvidenceUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app=QApplication.instance() or QApplication([])

    def setUp(self):
        self.f=fixtures.Service();self.f.setUp();self.path=self.f.path;self.root=self.f.f.root
        self.env=patch.dict(os.environ,REPLENISHMENT_DATA_DIR=str(Path(self.path).parent));self.env.start()
        self.reader=patch('history_tasks.HistoryReader.request');self.reader.start()
        self.host=SimpleNamespace(work_repository=SimpleNamespace(path=self.path),repository=SimpleNamespace(folder=Path(self.path).parent),closing=False)
        plan=batches.preview_new(self.path,[self.f.h],{'河北仓':fixtures.catalog(stock=100)})
        saved=batches.execute(self.path,plan);self.bid=saved['batch_id'];self.gid=saved['generated'][0]
        self.backend=SimpleNamespace(**{name:Mock(return_value='chosen.xlsx') for name in ('export_preview','export_batch','export_generations')})
        self.backend_patch=patch.dict(sys.modules,jst_stock_evidence=self.backend);self.backend_patch.start()
        from jst_results_ui import ResultsPage
        self.page=ResultsPage(self.host,[self.f.h]);self.page.timer.stop();self.app.processEvents()
        self.page.ready(results.read(self.path,self.root/'evidence-ui.sqlite3',self.f.h['day']))
        self.dialogs=[]

    def tearDown(self):
        import shiboken6
        for dialog in self.dialogs:dialog.reject();shiboken6.delete(dialog)
        self.page.timer.stop();self.page.reader.shutdown();self.page.close();shiboken6.delete(self.page);self.app.processEvents()
        self.backend_patch.stop();self.reader.stop();self.env.stop();self.f.tearDown()

    def generator(self,bid=None):
        from jst_ui import GenerationDialog
        with patch.object(GenerationDialog,'load_catalogs'):
            dialog=GenerationDialog(self.page,self.host,[self.f.h],bid);self.dialogs.append(dialog);self.app.processEvents()
        return dialog

    def direct_run(self,parent,fn,args,*a,**kw):return fn(*args)

    def test_unpreviewed_new_batch_explains_requirement_without_exporting_default(self):
        dialog=self.generator();dialog.catalogs={'河北仓':{'catalog':fixtures.catalog(stock=900)}}
        with patch('jst_ui.QMessageBox.information') as message,patch('jst_ui.export_locations.save_file') as picker:
            dialog.evidence_btn.click()
        self.assertIn('先点击“预览库存分配”',message.call_args.args[2]);picker.assert_not_called()
        self.backend.export_preview.assert_not_called();self.backend.export_batch.assert_not_called()

    def test_preview_export_passes_exact_plan_and_leaves_ui_scope_unchanged(self):
        dialog=self.generator();plan={'payload':{'new_batch':{'stocks':{'河北仓':{'catalog':fixtures.catalog(stock=100)}}}}}
        dialog.plan=copy.deepcopy(plan);dialog.catalogs={'河北仓':{'catalog':fixtures.catalog(stock=900)}}
        before=copy.deepcopy(dialog.plan)
        with patch('jst_ui.export_locations.save_file',return_value=('chosen.xlsx','')),patch('jst_ui.run',side_effect=self.direct_run):dialog.evidence_btn.click()
        self.backend.export_preview.assert_called_once_with(self.path,before,'chosen.xlsx')
        self.backend.export_batch.assert_not_called();self.assertEqual(dialog.plan,before)
        self.assertIn('已导出',dialog.status.text())

    def test_existing_batch_without_preview_exports_its_frozen_batch(self):
        dialog=self.generator(self.bid)
        with patch('jst_ui.export_locations.save_file',return_value=('chosen.xlsx','')),patch('jst_ui.run',side_effect=self.direct_run):dialog.evidence_btn.click()
        self.backend.export_batch.assert_called_once_with(self.path,self.bid,'chosen.xlsx');self.backend.export_preview.assert_not_called()

    def test_cancelled_destination_does_not_call_backend(self):
        dialog=self.generator(self.bid)
        with patch('jst_ui.export_locations.save_file',return_value=('','')):dialog.evidence_btn.click()
        self.backend.export_batch.assert_not_called()

    def test_results_checked_scope_uses_only_exact_generated_ids(self):
        self.page.chosen={results.key(self.f.h)};self.page.controls()
        with patch('jst_results_ui.export_locations.save_file',return_value=('chosen.xlsx','')),patch('jst_results_ui.run',side_effect=self.direct_run):self.page.evidence_btn.click()
        self.backend.export_generations.assert_called_once_with(self.path,[self.gid],'chosen.xlsx')
        self.assertEqual(self.page.evidence_btn.text(),'导出勾选采用仓资料（1单）');self.assertFalse(self.page.reviewing)

    def test_selected_pending_record_never_falls_back_to_other_generated_orders(self):
        pending=next(r for r in self.page.records if not r.get('gid'))
        self.page.chosen={results.key(pending['head'])};self.page.controls()
        self.assertFalse(self.page.evidence_btn.isEnabled());self.assertIn('（0单）',self.page.evidence_btn.text())
        with patch('jst_results_ui.export_locations.save_file') as picker:self.page.export_stock_evidence()
        picker.assert_not_called();self.backend.export_generations.assert_not_called()

    def test_no_selection_exports_only_current_visible_generated_records(self):
        self.page.chosen.clear();self.page.controls()
        with patch('jst_results_ui.export_locations.save_file',return_value=('chosen.xlsx','')),patch('jst_results_ui.run',side_effect=self.direct_run):self.page.evidence_btn.click()
        self.backend.export_generations.assert_called_once_with(self.path,[self.gid],'chosen.xlsx')
        self.assertEqual(self.page.evidence_btn.text(),'导出当前筛选采用仓资料（1单）')
        self.page.search.setText('does-not-match');self.assertFalse(self.page.evidence_btn.isEnabled())

    def test_results_batch_view_explicitly_exports_whole_batch(self):
        self.page.batch_id=self.bid;self.page.controls()
        with patch('jst_results_ui.export_locations.save_file',return_value=('chosen.xlsx','')),patch('jst_results_ui.run',side_effect=self.direct_run):self.page.evidence_btn.click()
        self.backend.export_batch.assert_called_once_with(self.path,self.bid,'chosen.xlsx')
        self.backend.export_generations.assert_not_called();self.assertEqual(self.page.evidence_btn.text(),'导出本批采用仓资料')

    def test_result_export_failure_is_readable_and_unlocks_controls(self):
        self.backend.export_generations.side_effect=ValueError('库存依据摘要不一致')
        with patch('jst_results_ui.export_locations.save_file',return_value=('chosen.xlsx','')),patch('jst_results_ui.run',side_effect=self.direct_run),patch('lifecycle_ui.warn') as notice:self.page.evidence_btn.click()
        notice.assert_called_once();self.assertIn('库存依据摘要不一致',self.page.status.text());self.assertFalse(self.page.reviewing)
        self.assertTrue(self.page.evidence_btn.isEnabled())


    def test_cancelled_worker_reports_cancellation_without_error_popup(self):
        from runtime_support import Cancelled
        self.backend.export_generations.side_effect=Cancelled()
        with patch('jst_results_ui.export_locations.save_file',return_value=('chosen.xlsx','')),patch('jst_results_ui.run',side_effect=self.direct_run),patch('lifecycle_ui.warn') as notice:self.page.evidence_btn.click()
        notice.assert_not_called();self.assertIn('已取消',self.page.status.text());self.assertFalse(self.page.reviewing)
        dialog=self.generator(self.bid);self.backend.export_batch.side_effect=Cancelled()
        with patch('jst_ui.export_locations.save_file',return_value=('chosen.xlsx','')),patch('jst_ui.run',side_effect=self.direct_run),patch('jst_ui.warn') as notice:dialog.evidence_btn.click()
        notice.assert_not_called();self.assertIn('已取消',dialog.status.text())


if __name__=='__main__':unittest.main()
