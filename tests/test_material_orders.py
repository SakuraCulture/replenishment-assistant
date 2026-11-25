import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import openpyxl

import jst_orders
import material_orders
import material_requests
import normal_orders
import order_lifecycle
import warehouse_catalog
import work_repository as wr


SKU = '001592321230000005'
HB = '河北仓'
SD = '山东仓'


def catalog(root, wh, stock, *, postfix='', sku=SKU, step=12):
    source = Path(root)/(wh+postfix+'.xlsx')
    book = openpyxl.Workbook()
    page = book.active
    page.append(['商品编码','商品名','产品分类','启用','基本售价','出库箱规','可用数',
                 '商品创建日期','商品标签','主仓实际库存','商品单位'])
    page.append([sku,'封箱胶带','门店耗材','启用','2.50',step,stock,'2026-10-01','',stock,'卷'])
    page.append(['@ZERO','扎带','门店耗材','备用','0.80',1,0,'2026-10-01','',0,'根'])
    page.append(['@DISABLED','旧标签','门店耗材','禁用','5',1,8,'2026-10-01','',8,'张'])
    page.append(['@OTHER','果汁','休闲食品','启用','3',1,10,'2026-10-01','',10,'瓶'])
    book.save(source)
    book.close()
    repository = warehouse_catalog.Repository(Path(root)/'settings')
    value = warehouse_catalog.import_catalog(source,wh)
    repository.save(value)
    return value


def seed(root):
    work = wr.Repository(Path(root)/'settings')
    with closing(work.connect()) as db, db:
        db.execute('''CREATE TABLE normal_orders(day TEXT NOT NULL,store TEXT NOT NULL,kind TEXT NOT NULL,
                      oid TEXT NOT NULL UNIQUE,number TEXT NOT NULL UNIQUE,current INTEGER NOT NULL,
                      PRIMARY KEY(day,store,kind))''')
        db.execute('INSERT INTO normal_orders VALUES(?,?,?,?,?,?)',(wr.today(),'A','normal_model','old-a','BH-OLD-A',1))
        db.executemany('INSERT INTO stores VALUES(?,?)',[(s,wr.dumps({'name':s+'店'})) for s in ('A','B')])
    normal_orders.Repository(work)
    catalog(root,SD,100)
    catalog(root,HB,145)
    return work.path


def request(path, store, wh, qty, sku=SKU):
    with closing(wr.Repository.open_existing(path).connect()) as db, db:
        req, version = material_requests.save(db,store,wh,[{'sku':sku,'quantity':qty}])
    return dict(request=req,version=version,sku=sku)


class MaterialOrders(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = seed(self.tmp.name)

    def test_migration_multibatch_stock_and_resume(self):
        selected = [request(self.path,'A',SD,70),request(self.path,'B',SD,50),request(self.path,'A',HB,16)]
        plan = material_orders.preview(self.path,selected)
        rows = plan['payload']['rows']
        self.assertEqual([(r['warehouse'],r['before'],r['quantity'],r['after'],r['missing']) for r in rows],
                         [(SD,'100','70','30','0'),(HB,'145','16','129','0'),(SD,'30','30','0','20')])
        self.assertEqual(rows[0]['unit'],'卷')
        with closing(wr.read_connection(self.path)) as db:
            self.assertFalse(jst_orders.exists(db))
        batch = material_orders.register(self.path,plan)
        catalog(self.tmp.name,SD,60,postfix='-new')
        self.assertEqual(material_orders.pending(self.path)[0]['id'],batch)
        first = material_orders.commit(self.path,batch)
        self.assertEqual(first,material_orders.commit(self.path,batch))
        with closing(wr.read_connection(self.path)) as db:
            self.assertEqual(db.execute('SELECT kind,oid,number,current FROM normal_orders WHERE oid=?',('old-a',)).fetchone(),
                             ('normal_model','old-a','BH-OLD-A',1))
            self.assertEqual(db.execute('SELECT count(*) FROM jst_reservations').fetchone()[0],3)
            self.assertEqual(db.execute('SELECT count(*) FROM normal_orders WHERE kind=?',('material',)).fetchone()[0],2)
            with self.assertRaises(sqlite3.OperationalError):
                db.execute('DELETE FROM normal_orders')
        followup = material_orders.preview(self.path,[request(self.path,'A',HB,8)])
        self.assertEqual(followup['payload']['rows'][0]['before'],'129')
        second = material_orders.commit(self.path,material_orders.register(self.path,followup))
        self.assertNotEqual(first['orders'][0]['head']['oid'],second['orders'][0]['head']['oid'])
        self.assertEqual(len(material_orders.pending(self.path)),0)

    def test_request_import_text_replay_and_ineligible_rows(self):
        source = Path(self.tmp.name)/'requests.xlsx'
        book = openpyxl.Workbook()
        page = book.active
        page.append(material_requests.HEADERS)
        for sku in (SKU,'@ZERO','@DISABLED','@OTHER'):
            page.append(('A',SD,sku,2))
        book.save(source)
        book.close()
        scan = material_requests.scan(source)
        first = material_requests.import_scanned(self.path,scan)
        self.assertEqual(first,material_requests.import_scanned(self.path,scan))
        rid, version = first[0]
        plan = material_orders.preview(self.path,[dict(request=rid,version=version,sku=sku) for sku in (SKU,'@ZERO','@DISABLED','@OTHER')])
        self.assertEqual([(r['sku'],r['quantity'],r['issue']) for r in plan['payload']['rows']],
                         [(SKU,'2',False),('@ZERO','0',False),('@DISABLED','0',True),('@OTHER','0',True)])
        with self.assertRaisesRegex(ValueError,'待核'):
            material_orders.register(self.path,plan)
        result = material_orders.commit(self.path,material_orders.register(self.path,plan,accept_issues=True))
        head = order_lifecycle.full_head(self.path,result['orders'][0]['head'])
        self.assertEqual(list(head['products']),[SKU])
        self.assertEqual(head['products'][SKU]['quantity'],2)

    def test_stock_is_shared_with_original_replenishment_commit(self):
        from test_jst import Service

        ordinary = Service()
        ordinary.setUp()
        self.addCleanup(ordinary.tearDown)
        path = ordinary.path
        with closing(wr.Repository.open_existing(path).connect()) as db, db:
            db.execute('INSERT OR IGNORE INTO stores VALUES(?,?)',('T',wr.dumps({'name':'测试店'})))
        stock = catalog(ordinary.f.root,HB,100,sku='0001',step=1)
        jst_orders.commit_refresh(path,jst_orders.prepare_refresh(path,HB,catalog=stock))
        original = ordinary.plan()
        choice = request(path,'T',HB,70,'0001')
        material = material_orders.preview(path,[choice])
        self.assertEqual(material['payload']['rows'][0]['quantity'],'70')
        confirmed = material_orders.commit(path,material_orders.register(path,material))
        rejected = jst_orders.commit(path,original)
        self.assertFalse(rejected['generated'])
        self.assertIn('共享仓库存占用',rejected['errors'][0]['error'])
        current = ordinary.plan()
        self.assertEqual(current['payload']['plans'][0]['rows'][0]['quantity'],'30')
        ordinary_result = jst_orders.commit(path,current)
        self.assertEqual(len(ordinary_result['generated']),1)
        extra = request(path,'T',HB,8,'0001')
        self.assertEqual(material_orders.preview(path,[extra])['payload']['rows'][0]['before'],'0')
        jst_orders.cancel_generated(path,[confirmed['orders'][0]['gid']],'合成测试未下单撤销')
        self.assertEqual(material_orders.preview(path,[extra])['payload']['rows'][0]['before'],'70')

    def test_dual_warehouse_platform_identity_and_payment(self):
        import aoxiang_purchase
        import jst_auto_order
        import jst_fulfillment
        import jst_one_click
        from jst_one_click_fixture import Component
        from test_jst_auto_order import AutoOrders

        selected = [request(self.path,'A',wh,qty) for wh,qty in ((SD,12),(HB,16))]
        plan = material_orders.preview(self.path,selected)
        result = material_orders.commit(self.path,material_orders.register(self.path,plan))
        heads = [r['head'] for r in result['orders']]
        fixture = AutoOrders()
        fixture.path = self.path
        for head in heads:
            fixture.configure(head)
        component = Component(Path(self.tmp.name)/'platform-fixture')
        prepared = jst_auto_order.prepare(self.path,result['orders'])
        self.assertFalse(prepared['failed'],prepared)
        previewed = jst_auto_order.run_browser(self.path,prepared['ready'],component,'preview')
        self.assertTrue(all(row['ok'] for row in previewed),previewed)
        component.drop_import_response = True
        preflight = jst_one_click.preflight(self.path,heads)
        ready = preflight['ready']
        self.assertEqual([r['mode'] for r in ready],['order','order'],preflight)
        first = jst_one_click.execute(self.path,ready,component)
        self.assertEqual([r['status'] for r in first],['导入结果待核','导入结果待核'])
        recovered = jst_one_click.execute(self.path,jst_one_click.preflight(self.path,heads)['ready'],component)
        self.assertEqual([r['internal'] for r in recovered],['17001','17002'])
        self.assertEqual([r['online'] for r in first],[r['online'] for r in recovered])
        self.assertEqual(sum(c[0]=='order.import' for c in component.calls),2)
        configuration = dict(configured=True,enabled=True,account_binding=dict(account_id='TEST',merchant_code='MAT'),
                             store_mapping_version='isolated',targets=[dict(buyerWarehouseCode='A',displayName='A店')])
        suppliers = {HB:'HB-MAT',SD:'SD-MAT'}
        for head, row, qty in zip(heads,recovered,(12,16)):
            with self.assertRaises(ValueError):
                aoxiang_purchase.freeze_payment(self.path,head,configuration,suppliers)
            review, _ = jst_fulfillment.prepare_review(self.path,[row['online']])
            saved = order_lifecycle.commit(self.path,review)
            self.assertTrue(saved)
            purchase = aoxiang_purchase.freeze_payment(self.path,head,configuration,suppliers)
            self.assertEqual(purchase['supplier_code'],suppliers[head['warehouse']])
            self.assertEqual(purchase['rows'][0]['sku'],SKU)
            self.assertEqual(int(purchase['total_quantity']),qty)
            self.assertEqual(order_lifecycle.snapshot(self.path,head)['status'],'待付款')
            component.orders[row['online']]['native_status']='发货中(打单拣货)'
            jst_fulfillment.run_order_payment(self.path,[row['online']],component)
            self.assertEqual(order_lifecycle.snapshot(self.path,head)['status'],'待收货')

    def test_material_order_merge_to_legacy_header_schema(self):
        import order_merge
        import order_migration
        import purchases

        selected = [request(self.path,'A',SD,9),request(self.path,'A',HB,8)]
        plan = material_orders.preview(self.path,selected)
        saved = material_orders.commit(self.path,material_orders.register(self.path,plan))
        new_b = material_orders.preview(self.path,[request(self.path,'A',SD,7)])
        later = material_orders.commit(self.path,material_orders.register(self.path,new_b))
        target = Path(self.tmp.name)/'legacy-target'
        work = wr.Repository(target)
        with closing(work.connect()) as db, db:
            db.execute('''CREATE TABLE normal_orders(day TEXT NOT NULL,store TEXT NOT NULL,kind TEXT NOT NULL,
                          oid TEXT NOT NULL UNIQUE,number TEXT NOT NULL UNIQUE,current INTEGER NOT NULL,
                          PRIMARY KEY(day,store,kind))''')
        package = Path(self.tmp.name)/'materials.zip'
        order_migration.export(self.path.parent,package)
        self.assertEqual(order_migration.manifest(package)['format'],order_migration.MATERIAL_FORMAT)
        report = order_merge.merge(self.path.parent,target)
        material_parents = {r['head']['oid'] for r in saved['orders']+later['orders']}
        actual = {row['oid']:row for row in report['orders'] if row['oid'] in material_parents}
        self.assertEqual({r['status'] for r in actual.values()},{'ADD'},actual)
        copied = purchases.targets(work.path,current_only=True)
        self.assertEqual({h['number'] for h in copied},{r['head']['number'] for r in saved['orders']+later['orders']})
        self.assertEqual(len(material_orders.available_requests(work.path)),3)


if __name__ == '__main__':
    unittest.main()
