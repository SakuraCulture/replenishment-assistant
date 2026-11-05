import copy,json,os,sqlite3,sys,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch,Mock
from contextlib import closing
from threading import Event
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import admission,admission_overview as view
from runtime_support import Cancelled


def records():
    return {'A':{'qualification':{'state':'已核验','reviewer':'张三','verified_on':'2026-01-01','expires':'2026-11-05','source':{'original':{'门店名称':'甲店'}}},
                 'refusals':{'001':{'name':'甲商品','reason':'滞销','active':True},'002':{'reason':'临时','active':True,'expires':'2026-11-04'},'003':{'reason':'解除','active':False}}},
            'ORPHAN':{'qualification':{'state':'无资质','source':{'original':{'门店名称':'独立店'}}}},
            'EXPIRED':{'qualification':{'state':'已核验','reviewer':'李四','verified_on':'2026-01-01','expires':'2026-11-04'}}}


class Fixture:
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.path=self.root/'workbench.sqlite3';self.values=records()
        with closing(sqlite3.connect(self.path)) as db,db:
            db.execute('CREATE TABLE stores(code TEXT PRIMARY KEY,data TEXT)');db.execute('CREATE TABLE store_admission(store TEXT PRIMARY KEY,payload TEXT)')
            db.execute('INSERT INTO stores VALUES (?,?)',('A',json.dumps({'name':'参数甲店'})))
            db.executemany('INSERT INTO store_admission VALUES (?,?)',[(k,json.dumps(v)) for k,v in self.values.items()])
    def tearDown(self):self.tmp.cleanup()
    def load(self,**kw):return view.load(self.path,day='2026-11-05',**kw)


class OverviewTests(Fixture,unittest.TestCase):
    def test_union_and_deleted_parameter_visibility(self):
        from aoxiang_directory import directory_version
        items=[{'code':'DIRECTORY','name':'目录店'}];(self.root/'store-directory.json').write_text(json.dumps({'schema':1,'items':items,'version':directory_version(items)}),'utf-8')
        d=self.load(known={'CURRENT':'本次店'});self.assertEqual(list(d['names']),['CURRENT','A','DIRECTORY','ORPHAN','EXPIRED']);self.assertEqual(d['names']['ORPHAN'],'独立店')
        with closing(sqlite3.connect(self.path)) as db,db:db.execute('DELETE FROM stores')
        d=self.load();self.assertIn('A',d['names']);self.assertEqual(d['names']['A'],'甲店')
    def test_pending_not_unqualified_and_expiry_inclusive(self):
        d=self.load(known={'PENDING':'新店'});rows={r[0]:r for r in d['qualification']}
        self.assertEqual(rows['PENDING'][2:4],['待核验','未登记']);self.assertEqual(rows['A'][2:4],['有效','已核验']);self.assertEqual(rows['EXPIRED'][2:4],['已到期','已核验'])
        for r in d['qualification']:
            context={'store':r[0],'record':self.values.get(r[0],{}),'day':d['day'],'revision':'test'}
            blocked=bool(admission.check(context,'none',{'product_tags':'二类医疗','product_tags_known':True})['reasons'])
            self.assertEqual(blocked,r[2]!='有效')
    def test_refusal_status_and_active_count(self):
        d=self.load();self.assertEqual([r[6] for r in d['refusals']],['拒收中','已到期','已解除']);self.assertEqual(d['qualification'][0][7],1)
        self.assertEqual(view.refusal_state({'expires':'2026-11-05'},d['day']),'拒收中')
    def test_search_state_and_focus_preserve_order(self):
        rows=self.load()['refusals'];self.assertEqual(view.select_rows(rows,'refusals','拒收中','参数甲 滞销'),[rows[0]])
        self.assertEqual(view.select_rows(rows,'refusals',focus={('A','003'),('A','001')}),[rows[0],rows[2]])
        self.assertEqual(view.select_rows(rows,'refusals',text='不存在'),[])
    def test_load_read_only_and_no_database_creation(self):
        before=self.path.read_bytes();self.load();self.assertEqual(self.path.read_bytes(),before)
        missing=self.root/'missing.sqlite3';self.assertEqual(view.load(missing)['qualification'],[]);self.assertFalse(missing.exists())
    def test_invalid_directory_is_visible_warning(self):
        (self.root/'store-directory.json').write_text('{}','utf-8');d=self.load();self.assertTrue(d['warnings']);self.assertEqual(len(d['qualification']),3)
    def test_invalid_admission_does_not_look_valid(self):
        with closing(sqlite3.connect(self.path)) as db,db:db.execute('UPDATE store_admission SET payload=? WHERE store=?',(json.dumps({'qualification':{'state':'已核验'}}),'A'))
        with self.assertRaisesRegex(ValueError,'门店 A'):self.load()
    def test_cancelled_read(self):
        cancel=Event();cancel.set()
        with self.assertRaises(Cancelled):self.load(cancel=cancel)
    def test_export_preserves_visible_values_types_order(self):
        from openpyxl import load_workbook
        rows=self.load()['refusals'];rows[0][2]='0001234567890123456789';rows[0][4]='=1+1'
        target=self.root/'out.xlsx';view.export(target,'refusals',rows[:1],{'搜索条件':'=test','条数':1});wb=load_workbook(target)
        self.assertEqual(wb.sheetnames,['门店拒收清单','查询范围']);ws=wb.worksheets[0]
        self.assertEqual(list(ws.values),[tuple(view.REF_HEADERS),tuple(rows[0])]);self.assertEqual(ws['E2'].data_type,'s');self.assertEqual(ws['C2'].data_type,'s');self.assertEqual(ws.freeze_panes,'C2');wb.close()
    def test_cancel_after_write_preserves_existing_target(self):
        target=self.root/'out.xlsx';target.write_bytes(b'original');cancel=Event()
        def progress(_):cancel.set()
        with self.assertRaises(Cancelled):view.export(target,'refusals',self.load()['refusals'],{},progress=progress,cancel=cancel)
        self.assertEqual(target.read_bytes(),b'original');self.assertEqual(list(self.root.glob('*.xlsx')),[target])


class OverviewUITests(Fixture,unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app=QApplication.instance() or QApplication([])
    def setUp(self):
        super().setUp();self.host=SimpleNamespace(work_repository=SimpleNamespace(path=self.path),store_widgets={},states={},readonly=False,worker=None,status=Mock(),invalidate=Mock())
    def test_readonly_editor_does_not_save_or_release(self):
        from admission_ui import Editor
        d=Editor(None,self.host,'A',readonly=True,section='refusals',sku='001');before=copy.deepcopy(d.value);d.release();d.save();self.assertEqual(d.value,before);self.assertTrue(d.qfields['note'].isReadOnly());d.close()
    def test_refusal_save_preserves_qualification_source(self):
        from admission_ui import Editor
        d=Editor(None,self.host,'A',section='refusals',sku='001');d.release()
        with patch('admission_ui.review_rows',return_value=True),patch('admission_ui.run') as run:
            d.save();saved=run.call_args.args[2][1]['A'];self.assertEqual(saved['qualification'],self.values['A']['qualification']);self.assertFalse(saved['refusals']['001']['active'])
        d.close()
    def test_qualification_edit_preserves_refusals_and_source(self):
        from admission_ui import Editor
        d=Editor(None,self.host,'A',section='qualification');d.qfields['note'].setText('新备注')
        with patch('admission_ui.review_rows',return_value=True),patch('admission_ui.run') as run:
            d.save();saved=run.call_args.args[2][1]['A'];self.assertEqual(saved['refusals'],self.values['A']['refusals']);self.assertEqual(saved['qualification']['source'],self.values['A']['qualification']['source'])
        d.close()
    def test_unchanged_qualification_editor_no_write(self):
        from admission_ui import Editor
        d=Editor(None,self.host,'A',section='qualification')
        with patch('admission_ui.run') as run:d.save();run.assert_not_called()
        d.close()
    def test_import_focus_resets_filter_to_show_exact_imported_keys(self):
        from admission_overview_ui import Overview
        d=Overview(None,self.host,'refusals');d.data=self.load();d.filter();self.assertEqual(len(d.filtered),1)
        with patch('admission_ui.import_records',return_value={'keys':[('A','002')]}),patch('admission_overview_ui.run',return_value=self.load()):d.import_file()
        self.assertEqual(d.state.currentText(),'全部');self.assertEqual([r[2] for r in d.filtered],['002']);d.show_all();self.assertEqual(len(d.filtered),3);d.close()
    def test_export_flushes_pending_search(self):
        from admission_overview_ui import Overview
        d=Overview(None,self.host,'refusals');d.data=self.load();d.state.setCurrentText('全部');d.search.setText('003')
        with patch('export_locations.save_file',return_value=(str(self.root/'out.xlsx'),'Excel')),patch('admission_overview_ui.run') as run:
            d.export_filtered();self.assertEqual([r[2] for r in run.call_args.args[2][2]],['003'])
        d.close()
    def test_readonly_view_allows_query_but_blocks_maintenance(self):
        from admission_overview_ui import Overview
        self.host.readonly=True;d=Overview(None,self.host);d.update_controls();self.assertFalse(d.edit_btn.isEnabled());self.assertFalse(d.import_btn.isEnabled());d.close()


if __name__=='__main__':unittest.main()
