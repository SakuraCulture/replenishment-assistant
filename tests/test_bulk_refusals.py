import copy,json,sqlite3,sys,unittest
from contextlib import closing
from pathlib import Path
from threading import Event
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from test_admission_overview import Fixture
import admission,admission_overview as service
from work_repository import Conflict
from runtime_support import Cancelled


class BulkTests(Fixture,unittest.TestCase):
    def test_independent_search_fields_and_exact_pasted_codes(self):
        rows = [
            ['001','北甲店','0001','原味饼干','包装破损','手动解除','拒收中'],
            ['002','北乙店','0001','原味饼干','包装破损','手动解除','拒收中'],
            ['001','北甲店','00010','原味饼干','包装破损','手动解除','拒收中'],
            ['001','北甲店','0002','原味饼干','缺资质','手动解除','拒收中'],
            ['003','南丙店','0002','原味饼干','北甲店包装破损','手动解除','拒收中'],
            ['001','北甲店','@000000123456789012345','原味饼干','包装破损','手动解除','已到期'],
        ]
        stores = service.selection_codes('001，002\n001')
        items = service.selection_codes('0001; @000000123456789012345')
        self.assertEqual(service.select_refusals(rows,'拒收中',None,'北甲','饼干','破损',stores,items), [rows[0]])
        self.assertEqual(service.select_refusals(rows,'全部',None,'北甲','饼干','破损',stores,items), [rows[0],rows[5]])
        self.assertEqual(service.select_refusals(rows,'拒收中',{('002','0001')},'北','饼干','破损',stores,items), [rows[1]])

    def test_edit_expires_without_reactivating_released_and_delete_retains_audit(self):
        before = copy.deepcopy(self.values['A'])
        edit = service.prepare_refusal_change(self.load(), [('A','002'),('A','003')], 'edit',
                                              {'reason': '  本店新原因  ', 'expires': None})
        self.assertEqual([r[-2:] for r in edit['rows']], [['已到期','拒收中'],['已解除','已解除']])
        service.commit_refusal_change(self.path, edit)
        edited = admission.snapshot(self.path, 'A')['record']
        self.assertEqual(edited['refusals']['001'], before['refusals']['001'])
        self.assertEqual(edited['refusals']['002']['reason'], '本店新原因')
        self.assertFalse(edited['refusals']['003']['active'])
        self.assertIsNone(edited['refusals']['002']['expires'])
        self.assertEqual(edited['qualification'], before['qualification'])
        removed = service.prepare_refusal_change(self.load(), [('A','001'),('A','003')], 'delete')
        service.commit_refusal_change(self.path, removed)
        self.assertEqual([r[2] for r in self.load()['refusals']], ['002'])
        with closing(sqlite3.connect(self.path)) as db:
            raw_before,raw_after = db.execute('SELECT before_value,after_value FROM admission_events ORDER BY id DESC LIMIT 1').fetchone()
        self.assertEqual(json.loads(raw_before), edited)
        self.assertEqual(json.loads(raw_after)['refusals'], {'002':edited['refusals']['002']})

    def test_edit_or_delete_stale_selection_cannot_partially_commit(self):
        before = {'refusals': {'001': {'reason': '另一门店'}}}
        admission.save(self.path, {'B':before}, {'B':{}})
        chosen = [('A','001'),('B','001')]
        pending = [service.prepare_refusal_change(self.load(), chosen, 'edit', {'reason':'重新核对'}),
                   service.prepare_refusal_change(self.load(), chosen, 'delete')]
        changed = {'refusals': {'001': {'reason': '其他窗口已更新'}}}
        admission.save(self.path, {'B':changed}, {'B':before})
        for batch in pending:
            with self.assertRaises(Conflict):service.commit_refusal_change(self.path, batch)
        self.assertEqual(admission.snapshot(self.path,'A')['record'], self.values['A'])
        self.assertEqual(admission.snapshot(self.path,'B')['record'], changed)

    def test_exact_store_sku_only_and_preserved_evidence(self):
        before=copy.deepcopy(self.values);data=self.load();p=service.prepare_release(data,[('A','001'),('A','001')])
        self.assertEqual(p['count'],1);self.assertEqual(data['records'],before);service.commit_release(self.path,p)
        after=admission.snapshot(self.path,'A')['record'];expected=copy.deepcopy(before['A']);expected['refusals']['001']['active']=False;self.assertEqual(after,expected)
        self.assertEqual(admission.snapshot(self.path,'ORPHAN')['record'],before['ORPHAN'])
    def test_conflict_rolls_back_entire_batch(self):
        other={'refusals':{'001':{'reason':'其他店','active':True}}};admission.save(self.path,{'B':other},{'B':{}})
        p=service.prepare_release(self.load(),[('A','001'),('B','001')]);changed=copy.deepcopy(other);changed['refusals']['001']['reason']='新原因';admission.save(self.path,{'B':changed},{'B':other})
        with self.assertRaises(Conflict):service.commit_release(self.path,p)
        self.assertTrue(admission.snapshot(self.path,'A')['record']['refusals']['001']['active']);self.assertEqual(admission.snapshot(self.path,'B')['record'],changed)
    def test_cancel_prepare_and_commit_does_not_write(self):
        e=Event();e.set();data=self.load();before=self.path.read_bytes()
        with self.assertRaises(Cancelled):service.prepare_release(data,[('A','001')],cancel=e)
        p=service.prepare_release(data,[('A','001')])
        with self.assertRaises(Cancelled):service.commit_release(self.path,p,cancel=e)
        self.assertEqual(self.path.read_bytes(),before)
    def test_missing_already_released_empty_are_rejected(self):
        for keys in ([],[('A','003')],[('A','missing')],[('missing','001')]):
            with self.assertRaises(ValueError):service.prepare_release(self.load(),keys)
    def test_expired_active_can_release_and_order_is_selection_order(self):
        p=service.prepare_release(self.load(),[('A','002'),('A','001')]);self.assertEqual([r[2] for r in p['rows']],['002','001']);service.commit_release(self.path,p)
        self.assertFalse(admission.snapshot(self.path,'A')['record']['refusals']['002']['active'])
    def test_commit_failure_rolls_back_all_rows_and_events(self):
        other={'refusals':{'001':{'reason':'其他店'}}};admission.save(self.path,{'B':other},{'B':{}})
        p=service.prepare_release(self.load(),[('A','001'),('B','001')])
        with closing(sqlite3.connect(self.path)) as db,db:
            count=db.execute('SELECT COUNT(*) FROM admission_events').fetchone()[0]
            db.execute("CREATE TRIGGER fail_b BEFORE UPDATE ON store_admission WHEN NEW.store='B' BEGIN SELECT RAISE(ABORT,'synthetic failure'); END;")
        with self.assertRaises(sqlite3.IntegrityError):service.commit_release(self.path,p)
        self.assertTrue(admission.snapshot(self.path,'A')['record']['refusals']['001']['active'])
        with closing(sqlite3.connect(self.path)) as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM admission_events').fetchone()[0],count)


class BulkUiTests(Fixture,unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app=QApplication.instance() or QApplication([])
    def window(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from admission_overview_ui import Overview
        host=SimpleNamespace(work_repository=SimpleNamespace(path=self.path),store_widgets={},states={},worker=None,readonly=False,status=Mock(),invalidate=Mock())
        d=Overview(None,host,'refusals');d.data=self.load();d.filter();return d
    def test_checkbox_identity_and_all_filter(self):
        from PySide6.QtCore import Qt
        d=self.window();d.state.setCurrentText('全部');d.select_all(True);self.assertEqual(d.model.keys(),[('A','001'),('A','002'),('A','003')])
        self.assertTrue(d.delete_btn.isEnabled());self.assertFalse(d.release_btn.isEnabled())
        d.model.setData(d.model.index(0,0),Qt.CheckState.Unchecked,Qt.ItemDataRole.CheckStateRole);self.assertEqual(d.model.keys(),[('A','002'),('A','003')]);d.close()
    def test_changed_filter_clears_hidden_selection(self):
        d=self.window();d.select_all(True);self.assertEqual(len(d.model.keys()),1);d.search_fields[2].setText('临时');d.filter();self.assertFalse(d.model.keys());d.close()
    def test_same_filter_preserves_checks_for_action_and_clear_all(self):
        d=self.window();d.select_all(True);d.filter();self.assertEqual(len(d.model.keys()),1);d.select_all(False);self.assertFalse(d.model.keys());d.close()
    def test_cancel_preview_no_commit(self):
        d=self.window();d.select_all(True);before=self.path.read_bytes()
        with patch('admission_overview_ui.run',side_effect=lambda parent,fn,args,*a,**kw:fn(*args)),patch('interaction_ui.review_rows',return_value=False):d.release_checked()
        self.assertEqual(self.path.read_bytes(),before);self.assertEqual(len(d.model.keys()),1);d.close()
    def test_readonly_cannot_check_or_commit(self):
        d=self.window();d.host.readonly=True;d.update_controls();d.select_all(True);self.assertFalse(d.model.keys())
        self.assertFalse(d.release_import_btn.isEnabled())
        with patch('admission_overview_ui.run') as run, patch('import_ui.choose') as choose:
            d.release_checked();d.import_release();run.assert_not_called();choose.assert_not_called()
        d.close()


class ReleaseImportTests(Fixture, unittest.TestCase):
    def sheet(self, rows):
        from openpyxl import Workbook
        book = Workbook()
        for row in rows:
            book.active.append(row)
        path = self.root/'release.xlsx'
        book.save(path)
        book.close()
        return path

    def test_preview_matches_pairs_and_explains_skips(self):
        from openpyxl import load_workbook
        long_code = '@0001234567890123456789'
        other = {'refusals': {sku: {'reason': '乙店', 'active': True} for sku in ('001', '1', long_code)}}
        admission.save(self.path, {'0002': other}, {'0002': {}})
        path = self.sheet([
            ['操作清单'], ['商品编码', '门店名称', '门店编码'],
            ['001', '名称不参与匹配', 'A'], ['001', None, 'A'],
            ['003', None, 'A'], ['001', None, 'UNKNOWN'], [None, None, 'A'],
            [1, None, '0002'], [long_code, None, '0002'], ['=1+1', None, '0002'],
        ])
        with closing(load_workbook(path)) as book:
            book.active['A8'].number_format = '000'
            book.save(path)
        before = self.path.read_bytes()
        plan = service.prepare_release_import(self.path, path)
        self.assertEqual(plan['keys'], [('A','001'), ('0002',long_code)])
        self.assertEqual([(r[0], r[3]) for r in plan['rows']], [
            (3,'将解除'), (4,'跳过 · 表内重复'), (5,'跳过 · 已解除'),
            (6,'跳过 · 未找到拒收记录'), (7,'跳过 · 编码无效（商品编码）'),
            (8,'跳过 · 编码无效（商品编码）'), (9,'将解除'), (10,'跳过 · 编码无效（商品编码）'),
        ])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(service.commit_release_import(self.path, plan)['count'], 2)
        expected = copy.deepcopy(self.values['A'])
        expected['refusals']['001']['active'] = False
        self.assertEqual(admission.snapshot(self.path,'A')['record'], expected)
        other['refusals'][long_code]['active'] = False
        self.assertEqual(admission.snapshot(self.path,'0002')['record'], other)
        after = self.path.read_bytes()
        replay = service.prepare_release_import(self.path, path)
        self.assertEqual(replay['keys'], [])
        with self.assertRaisesRegex(ValueError, '没有可解除'):
            service.commit_release_import(self.path, replay)
        self.assertEqual(self.path.read_bytes(), after)

    def test_export_can_be_used_for_release_by_exact_row(self):
        path = self.root/'refusals.xlsx'
        source = self.load()['refusals'][1:2]
        service.export(path, 'refusals', source, {'条数': 1})
        plan = service.prepare_release_import(self.path, path)
        self.assertEqual(plan['sheet'], '门店拒收清单')
        self.assertEqual(plan['keys'], [('A','002')])
        self.assertEqual(plan['rows'][0][4], '已到期')
        service.commit_release_import(self.path, plan)
        saved = admission.snapshot(self.path,'A')['record']['refusals']
        self.assertTrue(saved['001']['active'])
        self.assertFalse(saved['002']['active'])

    def test_changed_file_or_store_keeps_entire_import_uncommitted(self):
        other = {'refusals': {'001': {'reason': '原原因', 'active': True}}}
        admission.save(self.path, {'B': other}, {'B': {}})
        path = self.sheet([['门店编码','商品编码'], ['A','001'], ['B','001']])
        plan = service.prepare_release_import(self.path, path)
        self.sheet([['门店编码','商品编码'], ['A','002'], ['B','001']])
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, '预览后文件已变化'):
            service.commit_release_import(self.path, plan)
        self.assertEqual(self.path.read_bytes(), before)
        self.sheet([['门店编码','商品编码'], ['A','001'], ['B','001']])
        plan = service.prepare_release_import(self.path, path)
        changed = {'refusals': {'001': {'reason': '已在其他窗口更新', 'active': True}}}
        admission.save(self.path, {'B': changed}, {'B': other})
        before = self.path.read_bytes()
        with self.assertRaises(Conflict):
            service.commit_release_import(self.path, plan)
        self.assertEqual(self.path.read_bytes(), before)

    def test_rejects_missing_or_duplicate_key_headers(self):
        for headers, message in [(['门店编码','商品名称'],'两列表头'),
                                 (['门店编码','商品编码','门店编码'],'表头重复')]:
            path = self.sheet([headers, ['A','001']])
            with self.assertRaisesRegex(ValueError, message):
                service.prepare_release_import(self.path, path)

if __name__=='__main__':unittest.main()
