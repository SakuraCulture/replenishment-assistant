import copy,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import openpyxl
import first_fill as ff,warehouse_catalog as wc,new_products as np,new_product_focus as focus
from table_records import bridge_table
from test_preissue import row,result

def warehouse_fixture(root):
    catalogs={}
    for wh,stock,q in [('山东仓',0,18),('河北仓',100,30)]:
        p=root/(wh+'.xlsx');b=openpyxl.Workbook();s=b.active;s.append(wc.REQUIRED+['商品名称','商品标签',ff.FIELD])
        s.append(['0001','饮品',2,6,'启用',stock,'合成新品','',q]);b.save(p);b.close();catalogs[wh]=wc.import_catalog(p,wh)
    sales=[{'仓库/门店编码':'S1','仓库/门店名称':'合成店','商品编码':'0001','商品名称':'合成新品','日销':0,'周销':0,'月销':0,'可用库存':0,'在途库存':0,'缺货天数（天）':0,'补货状态':'正常'}]
    loaded=wc.prepare_records(bridge_table(list(sales[0]),sales,'原始销量'),catalogs)
    return loaded,catalogs

def base(loaded,primary,secondary=None):
    routed=wc.route_store(loaded,'S1',{'primary':primary,'secondary':secondary});d,m=routed['normalized'][0]
    r=row('0001',0,6,2);r.update(H=0,I=0,J=0,K=0,L=0,N=0,AU=d['AU'],_meta=copy.deepcopy(m))
    return result([r])

def monthly():
    value=focus.empty('2026-11');value['items']=[{'sku':'0001','focused':True,'name':'合成新品'}];value['revision']=focus.revision(value);return value

class WarehouseFirstFill(unittest.TestCase):
    def setUp(self):self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
    def test_import_roundtrip_and_same_sku_different_warehouse_routing(self):
        loaded,catalogs=warehouse_fixture(self.root);before=copy.deepcopy(loaded)
        self.assertEqual(ff.suggestion(base(loaded,'山东仓')['rows'][0])[0],18)
        self.assertEqual(ff.suggestion(base(loaded,'河北仓')['rows'][0])[0],30)
        self.assertEqual(ff.suggestion(base(loaded,'山东仓','河北仓')['rows'][0])[0],30)
        exported=self.root/'roundtrip.xlsx';wc.write_catalog(catalogs['山东仓'],exported)
        self.assertEqual(wc.import_catalog(exported,'山东仓')['items']['0001'][ff.FIELD],18);self.assertEqual(loaded,before)
    def test_source_missing_blank_zero_invalid_and_wrong_warehouse_never_fall_back_to_a_box(self):
        loaded,_=warehouse_fixture(self.root);r=base(loaded,'山东仓')['rows'][0]
        for raw in (None,'',0,-1,True,'bad',float('inf'),1e10):
            with self.subTest(raw=raw):
                test=copy.deepcopy(r);test['_meta']['first_fill']['raw']=raw;self.assertIsNone(ff.suggestion(test)[0])
        r['_meta']['first_fill']['warehouse']='河北仓';self.assertIsNone(ff.suggestion(r)[0])
        r['_meta']['first_fill']['warehouse']='山东仓';r['_meta']['first_fill']['catalog_revision']='different';self.assertIsNone(ff.suggestion(r)[0])
    def test_duplicate_first_fill_header_is_not_guessed_and_carton_budget_checks_are_unchanged(self):
        loaded,_=warehouse_fixture(self.root);p=self.root/'duplicate.xlsx';b=openpyxl.Workbook();s=b.active
        s.append(wc.REQUIRED+[ff.FIELD,ff.FIELD]);s.append(['0001','饮品',2,6,'启用',100,12,24]);b.save(p);b.close()
        duplicate=wc.import_catalog(p,'山东仓');item=duplicate['items']['0001']
        self.assertNotIn(ff.FIELD,item);self.assertIn('重复',item['_first_fill_issue']);self.assertEqual(item['基本售价'],2)
        evidence=ff.basis(duplicate,'山东仓','0001');candidate={'AU':'山东仓','_meta':{'first_fill':evidence,'product_revision':duplicate['revision']}}
        self.assertIsNone(ff.suggestion(candidate)[0]);self.assertIn('未自动择一',ff.suggestion(candidate)[1])
        r=base(loaded,'山东仓');before=copy.deepcopy(r)
        with self.assertRaisesRegex(ValueError,'倍数'):np.apply(r,{'0001':19})
        bounded=result(r['rows'],24);applied=np.apply(bounded,{'0001':18})
        self.assertLessEqual(applied['summary']['amount'],24);self.assertEqual(applied['new_products']['selection']['0001'],18)
        self.assertEqual(r,before);self.assertEqual(applied['rows'][0]['_meta']['first_fill']['raw'],18)

class FirstFillUi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app=QApplication.instance() or QApplication([])
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.loaded,_=warehouse_fixture(Path(self.temp.name))
    def dialog(self,r,current=None,p=None):
        from new_product_ui import NewProductDialog
        d=NewProductDialog(None,r,current or r,monthly() if p is None else p);d.show();self.app.processEvents()
        self.addCleanup(lambda:(d.close(),d.deleteLater(),self.app.processEvents()));return d
    def test_monthly_defaults_current_manual_and_previous_month_are_distinct(self):
        r=base(self.loaded,'山东仓');d=self.dialog(r);self.assertEqual(d.widgets[0][1].value(),18);self.assertFalse(d.widgets[0][0].isChecked())
        current=np.apply(r,{'0001':12});maintained=self.dialog(r,current);self.assertEqual(maintained.widgets[0][1].value(),12);self.assertTrue(maintained.widgets[0][0].isChecked())
        previous=self.dialog(r,p=focus.empty('2026-11'));self.assertEqual(previous.widgets[0][1].value(),6)
    def test_missing_quantity_can_be_maintained_but_never_silently_uses_minimum_unit(self):
        r=base(self.loaded,'山东仓');r['rows'][0]['_meta']['first_fill']['known']=False;d=self.dialog(r)
        self.assertIsNone(d.widgets[0][1].value());self.assertEqual(d.model.rowCount(),0);d.scope.setCurrentText('资料待核');self.assertEqual(d.model.rowCount(),1)
        self.assertEqual(d.model.data(d.model.index(0,8)),'待维护');d.widgets[0][1].setValue(12);d.widgets[0][0].setChecked(True);d.preview_btn.click()
        self.assertEqual(d.result(),1);self.assertEqual(d.selections(),{'0001':12})
    def test_invalid_carton_source_is_shown_and_preview_stays_open_with_clear_error(self):
        from PySide6.QtWidgets import QMessageBox
        r=base(self.loaded,'河北仓');r['rows'][0]['_meta']['first_fill']['raw']=31;d=self.dialog(r);d.scope.setCurrentText('全部新品');d.widgets[0][0].setChecked(True)
        self.assertEqual(d.widgets[0][1].value(),31);boxes=[]
        with patch.object(QMessageBox,'exec',new=lambda b:boxes.append(b.detailedText()) or QMessageBox.StandardButton.Ok):d.preview_btn.click()
        self.assertEqual(d.result(),0);self.assertTrue(boxes and '倍数' in boxes[0]);d.widgets[0][1].setValue(30);d.preview_btn.click();self.assertEqual(d.result(),1)
    def test_fractional_inventory_units_are_displayed_without_rounding_to_two_decimals(self):
        r=base(self.loaded,'山东仓');r['rows'][0]['BD']=.125;r['rows'][0]['_meta']['first_fill']['raw']='0.375';d=self.dialog(r)
        self.assertEqual(d.model.data(d.model.index(0,8)),'0.375');self.assertEqual(d.model.quantity_issue(0),'')
        self.assertIn('0.375',d.model.quantity_sources[0])
    def test_disabled_product_is_not_made_eligible_by_first_fill_quantity(self):
        r=base(self.loaded,'河北仓');r['rows'][0]['S']='禁用';d=self.dialog(r)
        self.assertEqual(d.rows,[]);self.assertEqual(d.widgets,[]);self.assertEqual(d.model.rowCount(),0)
