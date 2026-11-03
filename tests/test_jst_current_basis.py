"""Current admission, frozen stock, consistent export and bounded catalog reads."""
import copy,sys,unittest,threading,sqlite3
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import openpyxl
import jst_orders as orders,jst_exports as exports,jst_results as results
import warehouse_catalog as wc,import_templates,work_repository as wr
import test_jst
from test_jst import catalog

class CurrentBasis(unittest.TestCase):
    def setUp(self):
        self.f=test_jst.Service();self.f.setUp();self.path=self.f.path;self.root=self.f.f.root
        self.repo=wc.Repository(self.path.parent)
    def tearDown(self):self.f.tearDown()
    def save(self,tag='',stock=100,price=2,warehouse='河北仓'):
        c=catalog(warehouse,stock=stock,price=price);c['items']['0001']['商品标签']=tag;c['revision']=wc.revision(c['items']);self.repo.save(c);return c
    def read(self):return results.read(self.path,self.root/'cache.sqlite3',self.f.h['day'])
    def test_current_medical_tag_cannot_be_replaced_by_old_stock(self):
        c=self.save('必卖,二类医疗器械,其他标签');p=orders.prepare_checked(self.path,[self.f.h]);v=p['payload']['plans'][0]
        self.assertEqual(v['rows'][0]['quantity'],'0');self.assertIn('二类医疗',v['rows'][0]['reason'])
        self.assertEqual(v['admission']['catalog_revisions']['河北仓'],c['revision'])
        self.assertNotEqual(v['catalog']['revision'],c['revision'])
        self.assertFalse(orders.commit(self.path,p,True)['generated']);self.assertEqual(self.f.balances(),100)
    def test_current_tags_do_not_replace_frozen_stock_or_price(self):
        self.save('',stock=1,price=99);p=orders.prepare_checked(self.path,[self.f.h]);r=p['payload']['plans'][0]['rows'][0]
        self.assertEqual((r['quantity'],r['price']),('40','2'));self.assertFalse(orders.commit(self.path,p)['errors']);self.assertEqual(self.f.balances(),60)
    def test_without_default_uses_explicit_stock_catalog(self):
        p=orders.prepare_checked(self.path,[self.f.h]);self.assertEqual(p['payload']['plans'][0]['rows'][0]['quantity'],'40')
    def test_other_current_warehouse_medical_tag_blocks(self):
        self.save();self.save('二类医疗',warehouse='山东仓');p=orders.prepare_checked(self.path,[self.f.h])
        self.assertEqual(p['payload']['plans'][0]['rows'][0]['quantity'],'0')
    def test_ordinary_generation_panel_export_rejects_old_stock(self):
        _,gid=self.f.generated();self.f.refresh(20);self.assertEqual(results.export_plan(self.read())['entries'],[])
        # This is the service bound by the generation panel, before/after the fix.
        entry=getattr(exports,'export_checked',exports.export)
        try:r=entry(self.path,[gid],self.root/'ordinary')
        except (ValueError,wr.Conflict):pass
        else:self.assertEqual(r['files'],[],'普通入口错误导出了旧40件')
        self.assertFalse(list((self.root/'ordinary').glob('*.xlsx')))
    def test_explicit_history_export_keeps_quantity_and_marks_filename(self):
        _,gid=self.f.generated();self.f.refresh(20);r=results.export(self.path,results.export_plan(self.read(),allow_old=True),self.root/'history')
        file=Path(r['files'][0]['path']);self.assertIn('旧版本',file.name);self.assertIn(gid[:12],file.name)
        b=openpyxl.load_workbook(file);self.assertEqual(b.active['C2'].value,40);b.close()
    def test_two_orders_validate_whole_catalog_once(self):
        h=self.f.second_store();self.save();p=orders.prepare_checked(self.path,[self.f.h,h]);original=wc.Repository.catalogs;calls=[]
        def counted(repo):calls.append(1);return original(repo)
        with patch.object(wc.Repository,'catalogs',counted):r=orders.commit(self.path,p)
        self.assertFalse(r['errors']);self.assertEqual(len(r['generated']),2);self.assertEqual(len(calls),1)
    def test_default_changes_between_orders_reject_remaining(self):
        h=self.f.second_store();self.save();p=orders.prepare_checked(self.path,[self.f.h,h]);changed=[]
        def progress(value):
            if hasattr(value,'business_event') and not changed:changed.append(True);self.save('二类医疗')
        r=orders.commit(self.path,p,progress=progress)
        self.assertEqual(len(r['generated']),1);self.assertTrue(r['errors']);self.assertEqual(self.f.balances(),60)
    def test_downloaded_template_blank_tags_are_known(self):
        path=self.root/'template.xlsx';import_templates.write('catalog',path)
        b=openpyxl.load_workbook(path);s=b.worksheets[0];heads=[c.value for c in s[1]];self.assertIn('商品标签',heads)
        item=catalog()['items']['0001'];s.append([item.get(k) for k in heads]);b.save(path);b.close()
        c=wc.import_catalog(path,'河北仓');self.assertIn('商品标签',c['items']['0001']);self.assertIsNone(c['items']['0001']['商品标签'])
    def test_missing_tag_header_stays_unknown_with_early_warning(self):
        path=self.root/'legacy.xlsx';b=openpyxl.Workbook();s=b.active;s.append(wc.FIELDS);item=catalog()['items']['0001'];s.append([item.get(k) for k in wc.FIELDS]);b.save(path);b.close()
        c=wc.import_catalog(path,'河北仓');self.assertNotIn('商品标签',c['items']['0001'])
        warning=getattr(wc,'execution_warning',lambda _: '')(c);self.assertIn('商品标签',warning)
        self.repo.save(c);p=orders.prepare_checked(self.path,[self.f.h]);self.assertEqual(p['payload']['plans'][0]['rows'][0]['quantity'],'0')
    def test_changed_file_before_final_commit_rolls_back(self):
        self.save();p=orders.prepare_checked(self.path,[self.f.h]);original=orders.put_generation
        def change(db,plan):
            original(db,plan);c=catalog();c['items']['0001']['商品标签']='二类医疗';c['revision']=wc.revision(c['items'])
            # Simulate an external writer that does not use the supported save lock.
            wc.atomic_json(self.path.parent/'warehouse_catalogs/河北仓.json',c)
        with patch.object(orders,'put_generation',change):r=orders.commit(self.path,p)
        self.assertEqual(r['generated'],[]);self.assertTrue(r['errors']);self.assertEqual(self.f.balances(),100)
    def test_supported_catalog_update_waits_until_safe_commit_boundary(self):
        self.save();h=self.f.second_store();p=orders.prepare_checked(self.path,[self.f.h,h]);started=threading.Event();done=threading.Event();errors=[];threads=[]
        def writer():
            try:started.set();self.save('二类医疗')
            except Exception as exc:errors.append(exc)
            finally:done.set()
        def progress(value):
            from runtime_support import CommitProgress
            if isinstance(value,CommitProgress) and value.active and not threads:
                t=threading.Thread(target=writer);threads.append(t);t.start();self.assertTrue(started.wait(1));self.assertFalse(done.wait(.05))
            if hasattr(value,'business_event'):self.assertTrue(done.wait(3))
        try:r=orders.commit(self.path,p,progress=progress)
        finally:
            for t in threads:t.join(3)
        self.assertEqual(errors,[]);self.assertEqual(len(r['generated']),1);self.assertTrue(r['errors']);self.assertEqual(self.f.balances(),60)

if __name__=='__main__':unittest.main()
