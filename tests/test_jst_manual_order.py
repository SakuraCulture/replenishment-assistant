"""Only the identified original order can be adopted after manual import."""
from contextlib import closing
from decimal import Decimal
from pathlib import Path
import unittest

import jst_auto_order as auto
import jst_auto_order_store as store
import jst_manual_order as manual
import jst_orders
import lifecycle_store
import test_jst
import work_repository as wr
from jst_one_click_fixture import WAREHOUSES
from platform_import_manual_fixture import JstManual
import test_jst_auto_order


class ManualOrders(unittest.TestCase):
    def setUp(self):
        self.f=test_jst.Service()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.path=self.f.path
        self.head=self.f.h
        self.portal=JstManual(Path(self.path).parent)
        _,gid=self.f.generated()
        test_jst_auto_order.AutoOrders.configure(self,self.head)
        keys=auto.prepare(self.path,[{'gid':gid,'head':self.head}])
        self.assertFalse(keys['failed'])
        self.key,=keys['ready']

    def row(self, workbook=False):
        with closing(wr.read_connection(self.path)) as db:
            return store.read(db,self.key,workbook=workbook)

    def balances(self):
        with closing(wr.read_connection(self.path)) as db:
            return {wh:jst_orders.reservations(db,wh,set()) for wh in WAREHOUSES}

    def handoff(self):
        return manual.take_over(self.path,self.key,self.row()['sequence'],'测试经办','平台查实没有原单',self.portal)

    def preview(self, internal='60001'):
        return manual.preview(self.path,self.key,self.row()['sequence'],internal,'测试经办','核对原单明细',self.portal)

    def attach(self, draft):
        return manual.confirm(self.path,self.key,draft['id'],draft['digest'],self.portal)

    def test_handoff_exports_original_and_persists_exact_actual_order(self):
        reservations=self.balances()
        folder=Path(self.path).parent
        target=folder/'业务原件.xls'
        with self.assertRaisesRegex(ValueError,'确认接管'):
            manual.export_original(self.path,self.key,target,'manual')
        receipt=manual.export_original(self.path,self.key,folder/'核对用.xls','check')
        self.assertEqual(receipt['sha256'],self.row()['file_sha'])
        handoff=self.handoff()
        self.assertEqual(self.handoff(),handoff)
        self.assertEqual(store.list_orders(self.path)[0]['stage'],'MANUAL_HANDOFF')
        result=auto.run_browser(self.path,[self.key],self.portal,'submit')
        self.assertFalse(result[0]['ok'])
        first=manual.export_original(self.path,self.key,target,'manual')
        self.assertEqual(first,manual.export_original(self.path,self.key,target,'manual'))
        self.assertEqual(target.read_bytes(),self.row(workbook=True)['workbook'])
        self.portal.accepted(self.row(),'60001',changes=True)
        draft=self.preview()
        self.assertTrue(any(r['change']=='新增' for r in draft['differences']))
        self.assertIsNone(self.row()['internal'])
        with self.assertRaisesRegex(ValueError,'确认接管'):
            manual.export_original(self.path,self.key,folder/'二次手工.xls','manual')
        bound=self.attach(draft)
        self.assertEqual(self.attach(draft),bound)
        confirmed=self.row()
        self.assertEqual(confirmed['internal'],'60001')
        self.assertEqual(confirmed['proof']['manual_binding'],bound)
        self.assertEqual(self.balances(),reservations)
        with closing(wr.read_connection(self.path)) as db:
            h=confirmed['payload']['head']
            rows=lifecycle_store.decode(lifecycle_store.version_row(db,h,'jst_order'))
            self.assertEqual(rows['version'],1)
            self.assertIn('@00000001',{r['sku'] for r in rows['payload']['rows']})
            self.assertIsNone(lifecycle_store.version_row(db,h,'payment'))
            self.assertIsNone(lifecycle_store.version_row(db,h,'receipt'))
            n,=db.execute("SELECT count(*) FROM jst_events WHERE action='导出聚水潭原导入单'").fetchone()
            self.assertEqual(n,2)
        self.assertFalse(any(c[0]=='order.import' for c in self.portal.calls))

    def test_uncertain_import_and_changed_platform_or_warehouse_fail_closed(self):
        with closing(store.connection(self.path)) as db, db:
            store.update(db,self.row(),'IMPORT_UNKNOWN',reason='受理应答断开')
        with self.assertRaisesRegex(ValueError,'结果待核'):
            self.handoff()
        self.portal.accepted(self.row(),'60001')
        value=self.portal.orders[self.key]
        binding=value['binding']
        value['binding']={**binding,'warehouse':'山东仓'}
        with self.assertRaisesRegex(ValueError,'不同'):
            self.preview()
        value['binding']=binding
        with self.assertRaisesRegex(ValueError,'内部单号'):
            self.preview('60002')
        value['buyer_check']='masked_conflict'
        with self.assertRaisesRegex(ValueError,'隐号'):
            self.preview()
        value['buyer_check']='verified'
        draft=self.preview()
        value['items'][0]['qty']=str(Decimal(value['items'][0]['qty'])+1)
        value['items'][0]['amount']=str(Decimal(value['items'][0]['qty'])*Decimal(value['items'][0]['price']))
        value['amount']=str(sum(Decimal(row['amount']) for row in value['items']))
        with self.assertRaisesRegex(ValueError,'明细或状态已变化'):
            self.attach(draft)
        self.assertIsNone(self.row()['internal'])

    def test_authoritative_failure_retains_original_receipt_on_handoff(self):
        auto.run_browser(self.path,[self.key],self.portal,'preview')
        row=self.row()
        receipt={'online':self.key,'success_count':0,'fail_count':1,'text':'失败回执','observed_at':wr.now()}
        with closing(store.connection(self.path)) as db, db:
            store.update(db,row,'IMPORT_UNKNOWN',proof={**row['proof'],'import_receipt':receipt})
        self.assertEqual(self.handoff()['import_receipt'],receipt)
        self.assertEqual(self.row()['proof']['import_receipt'],receipt)
