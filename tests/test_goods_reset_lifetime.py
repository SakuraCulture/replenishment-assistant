"""Real attached native goods models reset without nested Qt reset state."""
import copy
import os
import unittest

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtCore import Qt,QPersistentModelIndex,qInstallMessageHandler
from PySide6.QtWidgets import QApplication,QTableView
import shiboken6
from ui_goods_fast import GoodsModel,FilterModel
from display_fields import COLUMNS


def row(sku,sales=30,category='A'):
    return dict(B='S1',E=sku,D='合成商品'+sku,AU='河北仓',G='包',BD=12,V=1,S='启用',
                H=1,I=7,J=sales,K=0,L=0,N=0,AN=2,AM=1,BH=10,BN=10,AS=12,BV=12,
                _source='资料匹配',_category=category,_meta={'blocking':[],'notes':[]},
                _audit={},_preview=False,_candidate_qty=12,_candidate_amount=12,_must_sell=False)


class GoodsResetLifetimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([])

    def setUp(self):
        self.messages=[]
        self.previous=qInstallMessageHandler(lambda kind,context,text:self.messages.append((str(kind),text)))
        self.view=QTableView();self.model=GoodsModel();self.model.setParent(self.view)
        self.proxy=FilterModel();self.proxy.setParent(self.view);self.proxy.setSourceModel(self.model)
        self.view.setModel(self.proxy);self.view.setSortingEnabled(True);self.view.resize(650,250);self.view.show()
        self.source_signals=[];self.proxy_signals=[]
        self.model.modelAboutToBeReset.connect(lambda:self.source_signals.append('begin'))
        self.model.modelReset.connect(lambda:self.source_signals.append('end'))
        self.proxy.modelAboutToBeReset.connect(lambda:self.proxy_signals.append('begin'))
        self.proxy.modelReset.connect(lambda:self.proxy_signals.append('end'))
        self.app.processEvents()

    def tearDown(self):
        try:
            self.view.setModel(None);self.proxy.setSourceModel(None)
            shiboken6.delete(self.proxy);shiboken6.delete(self.model);shiboken6.delete(self.view)
            self.app.processEvents()
        finally:qInstallMessageHandler(self.previous)

    def replace(self,rows):
        before=copy.deepcopy(rows);source_start=len(self.source_signals);proxy_start=len(self.proxy_signals)
        self.model.set_rows(rows);self.app.processEvents()
        self.assertEqual(self.source_signals[source_start:],['begin','end'])
        self.assertEqual(self.proxy_signals[proxy_start:],['begin','end'])
        self.assertIs(self.model.rows,rows)
        self.assertEqual(rows,before,'Native model reset must not mutate business records or order')
        self.assertEqual(self.model.rowCount(),len(rows));self.assertEqual(self.model.columnCount(),len(COLUMNS))
        self.assertEqual([self.model.headerData(c,Qt.Orientation.Horizontal) for c in range(len(COLUMNS))],[c[0] for c in COLUMNS])

    def no_reset_warning(self):
        bad=[text for _,text in self.messages if 'beginResetModel' in text or 'endResetModel' in text]
        self.assertEqual(bad,[],'Native Qt rejected a nested or unpaired model reset')

    def test_different_sizes_and_empty_have_one_real_reset_pair(self):
        for values in ([row('z'),row('a'),row('m')],[row('n')],[],[],[row('large'+str(i)) for i in range(19)]):
            with self.subTest(size=len(values)):self.replace(values)
        self.no_reset_warning()

    def test_persistent_source_and_proxy_indices_are_invalidated(self):
        self.replace([row('old-z'),row('old-a')])
        source=QPersistentModelIndex(self.model.index(0,0));proxy=QPersistentModelIndex(self.proxy.index(0,0))
        self.view.setCurrentIndex(self.proxy.index(0,0));self.assertTrue(source.isValid());self.assertTrue(proxy.isValid())
        self.replace([row('new')])
        self.assertFalse(source.isValid());self.assertFalse(proxy.isValid());self.assertFalse(self.view.currentIndex().isValid())
        self.no_reset_warning()

    def test_active_sort_and_filter_use_new_rows_without_reordering_business(self):
        self.proxy.sort(5,Qt.SortOrder.DescendingOrder)
        self.proxy.category='A';self.proxy.search='合成'
        for values in ([row('x',3),row('b',10),row('ignored',99,'B')],[],[row('y',8),row('z',1),row('q',4)]):
            self.replace(values);self.proxy.invalidateFilter();self.app.processEvents()
            expected=sorted((r for r in values if r['_category']=='A'),key=lambda r:r['J'],reverse=True)
            actual=[self.proxy.index(i,1).data() for i in range(self.proxy.rowCount())]
            self.assertEqual(actual,[r['E'] for r in expected])
            self.assertEqual([r['E'] for r in self.model.rows],[r['E'] for r in values])
            self.assertEqual(self.proxy.sortColumn(),5)
        self.no_reset_warning()

    def test_repeated_resets_keep_view_usable_for_selection_and_reading(self):
        for n in range(6):
            values=[row(f'{n}-{i}',i+1) for i in range(n%3+1)];self.replace(values)
            self.proxy.invalidateFilter();self.view.setCurrentIndex(self.proxy.index(0,0));self.app.processEvents()
            index=self.view.currentIndex();self.assertTrue(index.isValid())
            selected=self.proxy.mapToSource(index).row()
            self.assertEqual(index.data(),values[selected]['D'])
        self.no_reset_warning()


if __name__=='__main__':unittest.main()
