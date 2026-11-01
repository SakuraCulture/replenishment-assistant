import copy,sys,unittest,os
from pathlib import Path
from decimal import Decimal
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import jst_review as r


def plans():
    def row(sku,want,qty,price,amount):
        return dict(sku=sku,name=sku,demand=str(want),quantity=str(qty),price=price,amount=amount,reason='库存不足' if qty<want else '按回传分配')
    return [dict(head=dict(store='S',name='甲店',number='N1',warehouse='河北仓'),rows=[row('A',48,24,'2','48'),row('B',5,5,'3','15')]),
            dict(head=dict(store='S',name='甲店',number='N2',warehouse='山东仓'),rows=[row('A',24,24,'2','48'),row('C',4,0,None,None)]),
            dict(head=dict(store='T',name='乙店',number='N3',warehouse='河北仓'),rows=[row('A',10,10,'1','10')])]


class StoreReview(unittest.TestCase):
    def test_two_warehouses_group_store_and_exact_metric_identity(self):
        p=plans();before=copy.deepcopy(p);groups=r.stores(p);self.assertEqual(p,before)
        self.assertEqual([g['store'] for g in groups],['S','T'])
        m=groups[0]['metrics'];self.assertEqual((m['demand'],m['allocated'],m['reduced']),(159,111,48))
        self.assertEqual((m['sku'],m['arranged'],m['reduced_sku'],m['unallocated'],m['unknown']),(3,2,2,1,1))
        all_metrics=r.metrics([e for g in groups for e in g['entries']]);self.assertEqual(all_metrics['sku'],4)
        self.assertEqual(all_metrics['demand']-all_metrics['allocated'],all_metrics['reduced'])
    def test_unpriced_not_zero_and_zero_price_not_missing(self):
        p=plans();entry=(p[1]['head'],p[1]['rows'][1]);m=r.metrics([entry]);self.assertEqual(r.amount_text(m,'reduced'),'待核');self.assertIsNone(m['rate'])
        entry[1].update(price='0',amount='0');m=r.metrics([entry]);self.assertEqual(m['unknown'],0);self.assertEqual(r.amount_text(m,'demand'),'0.00')
    def test_empty_scope_and_precision(self):
        self.assertEqual(r.amount_text(r.metrics([]),'demand'),'0.00')
        p=plans()[0];p['rows']=[dict(sku='X',demand='3',quantity='1',price='0.1',amount='0.1')]
        self.assertEqual(r.stores([p])[0]['metrics']['reduced'],Decimal('0.2'))


class StoreReviewUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
        from PySide6.QtWidgets import QApplication
        cls.app=QApplication.instance() or QApplication([])
    def test_default_store_then_warehouse_and_reduction_filter(self):
        from jst_review_ui import AllocationReview
        w=AllocationReview();p=plans();before=copy.deepcopy(p);w.replace(p)
        self.assertEqual(w.pages.currentIndex(),0);self.assertEqual(w.store_model.rowCount(),2);self.assertEqual(w.model.rowCount(),0)
        w.open_store(w.store_model.index(0,0));self.assertEqual(w.model.rowCount(),4)
        w.kind.setCurrentText('有减量');self.assertEqual(w.model.rowCount(),2)
        w.warehouse.setCurrentText('河北仓');self.assertEqual(w.model.rowCount(),1)
        w.search.setText('没有');self.assertEqual(w.model.rowCount(),0)
        w.pages.setCurrentIndex(0);self.assertEqual(w.store_model.rowCount(),2);self.assertEqual(p,before)
        w.replace([],'原预览已失效');self.assertEqual(w.model.rowCount(),0);self.assertEqual(w.store_model.rowCount(),0)
        w.deleteLater();self.app.processEvents()

if __name__=='__main__':unittest.main()
