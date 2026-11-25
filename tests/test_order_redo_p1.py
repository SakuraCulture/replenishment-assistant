"""Same-store, same-SKU warehouses with isolated business SQLite and platform receipts."""
from contextlib import closing
import copy
from decimal import Decimal
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
from threading import Event
import unittest

import openpyxl

import aoxiang_receipts
import aoxiang_purchase as purchase
from aoxiang_client import ComponentError
from aoxiang_directory import Directory, atomic_json, file_hash
from aoxiang_purchase_tasks import prepare, validate_for_upload
from aoxiang_workflow_runner import Runner
from aoxiang_workflows import Workflows
import daily_batch
import jst_auto_order as jst
import jst_auto_order_store as orders
import jst_batches
import jst_fulfillment as fulfillment
import jst_order_store as generations
import lifecycle_batch
import lifecycle_io as io
import normal_delivery
import normal_orders
import order_lifecycle as lifecycle
import payment_shop_names
import purchases
from test_aoxiang_purchase import config
from test_aoxiang_purchase_tasks import COMPONENT, CONTRACT, NODE
from test_jst import catalog
from test_jst_auto_order import CONTACT, Portal
from test_signing_row_blocks import signing_fixture
import warehouse_catalog
from work_failures import WorkFailure
import work_repository as wr


WAREHOUSES = ('河北仓','山东仓')
STOCK = {'河北仓':100,'山东仓':145}
QUANTITY = {'河北仓':20,'山东仓':16}
PRICES = {'河北仓':2,'山东仓':3}
SUPPLIERS = {'河北仓':'SYNHB','山东仓':'SYNSD'}


def evidence(row):
    if os.environ.get('REPLENISHMENT_REDO_EVIDENCE') == '1':
        print('ACCEPTANCE '+json.dumps(row,ensure_ascii=False,sort_keys=True),flush=True)


class JST(Portal):
    def action(self,name,order,**kwargs):
        if name == 'payment_detail':
            self.calls.append((name,order['online']))
            rows = [{'sku':sku,'qty':qty,'price':price,'amount':str(Decimal(qty)*Decimal(price))}
                    for sku,qty,price in order['payload']['lines']]
            return {'online':order['online'],'binding':order['proof']['binding'],'found':True,
                    'buyer_check':'verified','contact_ok':True,**self.orders[order['online']],
                    'items':rows,'detail_count':len(rows),'currency':'CNY','freight':'0',
                    'amount':str(sum(Decimal(r['amount']) for r in rows))}
        return super().action(name,order,**kwargs)


class AX:
    """Runs the real host and Node dispatch guards against the same isolated DB."""
    def __init__(self, case):
        self.case = case
        self.packages = {}
        self.records = {}
        self.uploads = []
        self.observations = {}

    def register(self, package):
        id = package['id']
        self.packages[id] = package
        self.records[id] = {'id':id,'state':'RESERVED','uploadAttempted':False,
                            'taskId':None,'binding':None,'active':False}

    def guard(self, package, *, consume=False):
        script = """
import fs from 'node:fs';import {checkHostDispatch} from './src/purchase-host-guard.mjs';
const p=JSON.parse(fs.readFileSync(process.argv[1],'utf8'));
try{checkHostDispatch(p,{consume:process.argv[2]==='1',requestHash:'a'.repeat(64)});console.log(JSON.stringify({ok:true}));}
catch(e){console.log(JSON.stringify({ok:false,code:e.code,message:e.message}));}
"""
        r = subprocess.run([str(NODE),'--input-type=module','-e',script,package['package_file'],str(int(consume))],
                           cwd=COMPONENT,env={**os.environ,'REPLENISHMENT_BUSINESS_DB':str(self.case.path)},
                           check=True,capture_output=True,text=True,encoding='utf-8',timeout=20)
        return json.loads(r.stdout.splitlines()[-1])

    def request(self, action, **values):
        if action == 'configuration':
            return {**self.case.config,'purchase_pipeline':True}
        id = values['purchase_id']
        record = self.records[id]
        if action == 'purchase.status':
            return copy.deepcopy(record)
        if action == 'purchase.submit':
            if record['uploadAttempted']:
                raise AssertionError('Duplicate controlled AX submission')
            outcome = self.guard(self.packages[id],consume=True)
            if not outcome['ok']:
                raise ComponentError(outcome['code'],outcome['message'])
            self.uploads.append(id)
            record.update(state='ACCEPTED',uploadAttempted=True,taskId='TASK-'+str(len(self.uploads)))
            return copy.deepcopy(record)
        if action == 'purchase.start':
            record.update(state='BOUND',binding={'orderId':'PO-SYNTH-'+str(self.uploads.index(id)+1)})
            return copy.deepcopy(record)
        if action == 'receipt.status':
            return copy.deepcopy(self.observations[id])
        if action == 'receipt.start':
            return {'started': True}
        if action == 'receipt.synced':
            self.observations[id].update(state='SYNCED',hostReceipt=values['receipt'])
            return copy.deepcopy(self.observations[id])
        raise AssertionError(action)


class Dual:
    def __init__(self, root, *, operations=False):
        self.root = Path(root)
        self.work = wr.Repository(root/'settings')
        self.repo = normal_orders.Repository(self.work)
        self.path = self.work.path
        self.jst = JST()
        self.ax = AX(self)
        self.config = config()
        self.operations = operations
        self.workflow = Workflows(self.path.parent/'aoxiang', business_path=self.path)
        Directory(self.workflow.folder).save_suppliers(SUPPLIERS)
        self.catalogs = {wh:self.stock(wh,STOCK[wh]) for wh in WAREHOUSES}
        # Use distinct source signing revisions of the same parent. Each warehouse
        # has SKU 00000 and SKU 00001 without violating per-snapshot uniqueness.
        self.sign('河北仓')
        self.sign('山东仓')
        self.heads = {h['warehouse']:h for h in purchases.targets(self.path)}
        for wh, head in self.heads.items():
            data = {'names':{'T':'合成双仓店'},'records':{}}
            update = payment_shop_names.prepare(data,[{'store':'T','warehouse':wh,'value':'合成店铺'+wh}])
            payment_shop_names.save(self.path,update)
            jst.save_profile(self.path,'T',wh,{**CONTACT,'buyer':wh+'测试买家'},None)
        self.sequence = 0

    def sign(self, warehouse, quantity=24):
        payload = signing_fixture([('T',(warehouse,),quantity)],count=2)
        if self.operations:
            payload['lifecycle_flow'] = 2
        job = normal_delivery.job_head(self.work,payload['day'])
        previous = self.repo.headers()
        return self.repo.confirm(payload,['T'],job['revision'] if job else None,
                                 {'T':previous[0]['revision'] if previous else None},
                                 headers_only=True,warehouses={'T':[warehouse]})[0]

    def stock(self, warehouse, quantity):
        value = catalog(warehouse, stock=quantity, price=PRICES[warehouse])
        row = value['items'].pop('0001')
        value['items'] = {sku:{**row,'商品编码':sku} for sku in ('00000','00001')}
        value['revision'] = warehouse_catalog.revision(value['items'])
        warehouse_catalog.Repository(self.path.parent).save(value)
        return value

    def manual_file(self, wh, stage, quantity, amount=None, *, include_zero=True):
        head = self.heads[wh]
        self.sequence += 1
        path = self.root/f'{stage}-{self.sequence}.xlsx'
        book = openpyxl.Workbook()
        sheet = book.active
        fields = io.fields(stage)
        sheet.append(fields)
        for sku, qty in [('00000',quantity)]+([('00001',0)] if include_zero else []):
            record = {'补货单号':head['number'],'签发修订':head['revision'],'门店编码':'T','供货仓':wh,
                      '商品编码':sku,'商品名称':'合成商品','数量单位':'库存单位',io.QUANTITIES[stage]:qty,
                      io.DOCNUM[stage]:f'QA-{stage}-{self.sequence}',io.DOCDATE[stage]:head['day'],
                      '实际金额':amount if qty else 0,'币种':'CNY'}
            sheet.append([record.get(k) for k in fields])
        book.save(path)
        book.close()
        return path

    def manual(self, wh, stage, quantity, amount=None, *, include_zero=True):
        head = self.heads[wh]
        path = self.manual_file(wh, stage, quantity, amount, include_zero=include_zero)
        file, = io.inspect([path])
        spec = file['sheets'][0]
        scan = io.scan(stage,[{**file,**spec,'mapping':{k:i for i,k in enumerate(spec['headers'])}}])
        return lifecycle.commit(self.path,lifecycle.prepare_numbered(self.path,scan,head))

    def allocate(self, warehouses):
        preview = jst_batches.preview_new(self.path,[self.heads[w] for w in warehouses],
                                          {w:self.catalogs[w] for w in warehouses})
        assigned = jst_batches.execute(self.path,preview)
        if len(assigned['generated']) != len(warehouses):
            raise AssertionError(assigned)
        return dict(zip(warehouses,assigned['generated']))

    def begin(self):
        for wh in WAREHOUSES:
            self.manual(wh,'return',QUANTITY[wh])
            if self.operations:
                self.manual(wh,'operations',QUANTITY[wh])
        return self.allocate(WAREHOUSES)

    def order(self, wh, gid, *, disconnect=False):
        result = jst.prepare(self.path,[{'gid':gid,'head':self.heads[wh]}])
        if result['failed']:
            raise AssertionError(result)
        online, = result['ready']
        if online not in self.jst.orders:
            preview, = jst.process(self.path,[online],self.jst,'preview')
            if preview['stage']!='PREVIEWED':
                raise AssertionError(preview)
            self.jst.disconnected = disconnect
            response, = jst.process(self.path,[online],self.jst,'submit',
                                    prepare_payment=fulfillment.pull_order_payment)
            self.jst.disconnected = False
            if disconnect and response['stage'] != 'IMPORT_UNKNOWN':
                raise AssertionError(response)
            if not disconnect and response['stage'] != 'IMPORTED':
                raise AssertionError(response)
        return online

    def recover(self, online):
        return jst.process(self.path,[online],self.jst,'recover',
                           prepare_payment=fulfillment.pull_order_payment)[0]

    def pay(self, online):
        plan, _ = fulfillment.prepare_review(self.path,[online])
        return lifecycle.commit(self.path,plan)[0]

    def cancel(self, online):
        self.jst.orders[online].update(native_status='已取消',status='review')
        return self.recover(online)

    def generation(self, gid):
        with closing(wr.read_connection(self.path)) as db:
            return generations.read_generation(db,gid)[0]

    def remaining(self, gid):
        g = self.generation(gid)
        wh = g['head']['warehouse']
        with closing(wr.read_connection(self.path)) as db:
            scope = jst_batches.context(db,g['batch_id'])
            stock = scope['stocks'][wh]['catalog']['items']['00000']['可用数']
            used = jst_batches.reservations(db,g['batch_id'],wh).get('00000',0)
        return int(Decimal(stock)-used)

    def make_package(self, wh):
        template = COMPONENT/'test/fixtures/purchase-import-template.xlsx'
        contract = json.loads(CONTRACT.read_text('utf-8'))
        reference = {'status':'SUPPORTED','contractVersion':contract['version'],'schema':contract['schema'],
                     'sha256':file_hash(template),'file':str(template),'uiProductLimit':None,
                     'identity':{'accountId':'A','merchantCode':'M'},'attemptId':'isolated-redo',
                     'recordFile':str(self.root/'isolated-template.json')}
        atomic_json(reference['recordFile'],reference)
        package = prepare(self.path,self.heads[wh],self.config,SUPPLIERS,reference,
                          self.workflow.folder/'purchase-packages',CONTRACT)
        atomic_json(Path(package['package_file']).parent/'本地任务索引.json',package)
        self.ax.register(package)
        return package

    def queue(self, wh, package, preview=None):
        if preview is None:
            preview = self.workflow.prepare([self.heads[wh]])
        preview = self.workflow.update(preview['id'],state='READY',position=0,changes={
            'state':'PREPARED','package':{k:package[k] for k in ('id','package_file','package_sha256','plan_file','plan_sha256')}})
        return self.workflow.upload(preview['id'],[0],preview['sequence'])

    def runner(self):
        return Runner(self.workflow,self.ax,COMPONENT,self.path,cancel=Event(),delay=.001,
                      verification_wait=1,verification_interval=.001)

    def stage_receipt(self, package):
        p = package['intent']
        record = self.ax.records[p['id']]
        order_id = record['binding']['orderId']
        common = {'orderId':order_id,'store':'T','supplier':p['supplierCode'],'status':'已完成'}
        rows = [{**common,'sku':r['sku'],'name':r['name'],'unit':'库存单位','_sourceRow':i,
                 'quantity':r['quantity'],'amount':r['amount'],
                 'receivedQuantity':r['quantity'],'receivedAmount':r['amount']} for i,r in enumerate(p['rows'],2)]
        main = {**common,'skuCount':len(rows),'quantity':p['totalQuantity'],'amount':p['totalAmount'],
                'receivedQuantity':p['totalQuantity'],'receivedAmount':p['totalAmount']}
        files = []
        folder = Path(package['plan_file']).parent
        for source, values in [('D16',[main]),('D17',rows)]:
            path = folder/(source+'.xlsx')
            book = openpyxl.Workbook()
            sheet = book.active
            sheet.append(list(values[0]))
            for value in values:
                sheet.append(list(value.values()))
            book.save(path)
            book.close()
            files.append({'source':source,'file':str(path),'sha256':file_hash(path)})
        manifest = folder/'实收观察.json'
        atomic_json(manifest, {'schema':1,'operation':'purchase.receipt.observation','purchaseId':p['id'],
            'runId':p['id'],'identity':p['identity'],'basis':p['basis'],'orderId':order_id,'supplierCode':p['supplierCode'],
            'observedAt':'2026-11-22T02:00:00Z','terminal':True,'eligible':True,'main':main,
            'rows':rows,'issues':[],'files':files,'quantityBasis':'native_actual_inventory_quantity_no_conversion'})
        proof = {'file':str(manifest),'sha256':file_hash(manifest)}
        self.ax.observations[p['id']] = {'state':'READY','scope':{'orderId':order_id},'basis':p['basis'],
            'terminal':True,'terminalKind':'COMPLETED','summary':{'status':'已完成'},'manifest':proof}
        original = {**record,'basis':p['basis']}
        return original, proof

    def receive(self, package, *, verify_pending=None):
        original, proof = self.stage_receipt(package)
        pid = package['id']
        self.workflow.configure_schedules([original],5,True)
        pending = self.workflow.receipt([original])
        self.workflow.observe_schedule(pid,self.ax.observations[pid])
        if verify_pending:
            verify_pending(pending)
        job = self.runner().run(pending['id'])
        if job['items'][0]['state'] != 'SYNCED':
            raise AssertionError(job)
        repeated = aoxiang_receipts.synchronize(self.path,package,proof,self.config,SUPPLIERS)
        return job['items'][0]['result']['receipt'], repeated


class OrderRedoTests(unittest.TestCase):
    def dual(self, *, operations=False):
        tmp = tempfile.TemporaryDirectory(prefix='isolated-order-redo-')
        self.addCleanup(tmp.cleanup)
        return Dual(Path(tmp.name),operations=operations)

    def case_stamp(self, case, wh):
        from jst_cancellation import review_context
        with closing(wr.read_connection(case.path)) as db:
            return review_context(db,case.heads[wh])['stamp']

    def test_batch_return_replacement_preserves_platform_execution(self):
        from jst_cancellation import begin_redo
        from jst_order_guards import allocation_gate

        for linked, pending in (WAREHOUSES, WAREHOUSES[::-1]):
            with self.subTest(linked=linked, pending=pending):
                case = self.dual()
                gids = case.begin()
                heads = [case.heads[wh] for wh in (linked, pending)]
                keys = [case.order(linked, gids[linked]), case.order(pending, gids[pending], disconnect=True)]
                case.pay(keys[0])
                package = case.make_package(linked)
                payments = lifecycle.history(case.path, heads[0], 'payment')
                old_returns = [lifecycle.history(case.path, head, 'return')[0] for head in heads]
                with closing(wr.read_connection(case.path)) as db:
                    old_tasks = [orders.read(db, key) for key in keys]
                self.assertEqual([row['stage'] for row in old_tasks], ['IMPORTED', 'IMPORT_UNKNOWN'])
                case.jst.orders[keys[0]].update(native_status='已取消', status='review', buyer_check='unavailable')
                calls = list(case.jst.calls)

                files = [case.manual_file(linked, 'return', 12), case.manual_file(pending, 'return', 7)]
                documents = lifecycle_batch.read(files)['documents']
                receipt = lifecycle.commit(case.path, lifecycle_batch.prepare(case.path, documents, heads))
                self.assertEqual([(r['number'], r['version'], r['changed']) for r in receipt],
                                 [(head['number'], 2, True) for head in heads])
                repeated = lifecycle.commit(case.path, lifecycle_batch.prepare(case.path, documents, heads))
                self.assertEqual([(r['version'], r['changed']) for r in repeated], [(2, False), (2, False)])
                for head, old, quantity in zip(heads, old_returns, ('12', '7')):
                    new, retained = lifecycle.history(case.path, head, 'return')
                    self.assertEqual(retained, old)
                    self.assertEqual(new['payload']['rows'][0]['base_quantity'], quantity)
                self.assertEqual(lifecycle.history(case.path, heads[0], 'payment'), payments)
                self.assertEqual(case.jst.calls, calls)
                with closing(wr.read_connection(case.path)) as db:
                    self.assertEqual([orders.read(db, key) for key in keys], old_tasks)
                    for wh, head in zip((linked, pending), heads):
                        self.assertEqual(generations.active(db, head), gids[wh])
                        self.assertEqual(case.remaining(gids[wh]), STOCK[wh] - QUANTITY[wh])
                        reason, _ = allocation_gate(db, head, gids[wh], correction=True)
                        self.assertIn('聚水潭', reason)
                with self.assertRaisesRegex(ValueError, '回传对照依据变化'):
                    validate_for_upload(case.path, package, case.config, SUPPLIERS)
                with self.assertRaisesRegex(ValueError, '明确返回'):
                    begin_redo(case.path, keys[0])

                case.jst.orders[keys[0]]['buyer_check'] = 'verified'
                self.assertEqual(case.recover(keys[0])['stage'], 'CANCELLED')
                begin_redo(case.path, keys[0])
                new_gid = case.allocate([linked])[linked]
                self.assertEqual(case.generation(new_gid)['rows'][0]['quantity'], '12')
                self.assertEqual(case.remaining(new_gid), STOCK[linked] - 12)
                self.assertEqual(case.remaining(gids[pending]), STOCK[pending] - QUANTITY[pending])
                self.assertEqual([action for action, _ in case.jst.calls].count('import'), 2)

    def test_replacing_return_prevents_old_platform_candidate_becoming_current_payment(self):
        case = self.dual()
        warehouse = '河北仓'
        gid = case.begin()[warehouse]
        original = case.order(warehouse, gid)
        payment, _ = fulfillment.prepare_review(case.path, [original])

        result, = case.manual(warehouse, 'return', 12)
        self.assertEqual(result['version'], 2)
        with self.assertRaisesRegex(wr.Conflict, '回传或运营签发依据已更新'):
            fulfillment.prepare_review(case.path, [original])
        with self.assertRaisesRegex(wr.Conflict, '回传或运营签发依据已更新'):
            lifecycle.commit(case.path, payment)
        self.assertEqual(lifecycle.history(case.path, case.heads[warehouse], 'payment'), [])

    def test_unpaid_cancel_redo_full_two_warehouses_and_stock_refresh(self):
        from jst_cancellation import begin_redo
        examples = [(warehouse,refresh,operations) for warehouse in WAREHOUSES
                    for refresh,operations in ((False,False),(True,False),(False,True))]
        for changed, refresh, operations in examples:
            with self.subTest(changed=changed,refresh=refresh,operations=operations):
                case = self.dual(operations=operations)
                other = next(w for w in WAREHOUSES if w != changed)
                original = case.begin()
                first = case.order(changed,original[changed])
                second = case.order(other,original[other])
                original_balance = STOCK[changed]-QUANTITY[changed]
                self.assertEqual(case.remaining(original[changed]),original_balance)
                self.assertEqual(case.pay(second)['version'],1)
                with self.assertRaisesRegex(ValueError,'明确返回'):
                    begin_redo(case.path,first)
                remote = case.jst.orders.pop(first)
                self.assertNotEqual(case.recover(first)['stage'],'CANCELLED')
                case.jst.orders[first] = remote
                for status in ('待付款','异常'):
                    remote.update(native_status=status,status='review')
                    self.assertEqual(case.recover(first)['stage'],'REVIEW')
                    with self.assertRaises(ValueError):
                        begin_redo(case.path,first)
                self.assertEqual(case.remaining(original[changed]),original_balance)
                self.assertEqual(case.cancel(first)['stage'],'CANCELLED')
                self.assertEqual(case.remaining(original[changed]),original_balance)
                stock = STOCK[changed]-5 if refresh else STOCK[changed]
                if refresh:
                    case.catalogs[changed] = case.stock(changed,stock)
                archived = Path(case.root/'old-library.sqlite')
                with closing(sqlite3.connect(case.path)) as src, closing(sqlite3.connect(archived)) as dst:
                    src.backup(dst)
                with closing(wr.read_connection(case.path)) as db:
                    old = orders.read(db,first,workbook=True)
                redo = begin_redo(case.path,first)
                self.assertEqual(redo['released'],{'00000':str(QUANTITY[changed])})
                self.assertEqual(case.remaining(original[changed]),STOCK[changed])
                self.assertEqual(begin_redo(case.path,first),redo)
                with closing(wr.read_connection(case.path)) as db:
                    self.assertEqual(orders.read(db,first,workbook=True),{**old,'historical':True})
                    self.assertIsNone(orders.read_warehouse_order(db,case.heads[changed]))
                case.manual(changed,'operations' if operations else 'return',12)
                new = case.allocate([changed])[changed]
                after = case.generation(new)['rows'][0]
                self.assertEqual((after['stock'],after['before'],after['quantity']),(str(stock),str(stock),'12'))
                self.assertEqual(case.remaining(new),stock-12)
                successor = case.order(changed,new,disconnect=True)
                self.assertEqual(successor,redo['online'])
                self.assertNotEqual(successor,first)
                reopened = JST()
                reopened.orders = case.jst.orders
                submitted = list(case.jst.calls)
                case.jst = reopened
                self.assertEqual(case.recover(successor)['stage'],'IMPORTED')
                self.assertEqual(case.order(changed,new),successor)
                with closing(wr.read_connection(case.path)) as db:
                    new_task = orders.read(db,successor)
                case.jst.orders[first].update(native_status='已付款待审核',status='awaiting')
                self.assertEqual(case.recover(first)['stage'],'CANCELLED')
                self.assertEqual(case.pay(successor)['version'],1)
                self.assertEqual(case.remaining(new),stock-12)
                self.assertEqual(begin_redo(case.path,first),redo)
                for wh, online, gid in ((changed,successor,new),(other,second,original[other])):
                    package = case.make_package(wh)
                    queue = case.queue(wh,package)
                    bound = case.runner().run(queue['id'])['items'][0]
                    self.assertEqual(bound['state'],'BOUND',bound)
                    saved, repeat = case.receive(package)
                    self.assertEqual((saved['version'],repeat['version'],repeat['changed']),(1,1,False))
                    h = case.heads[wh]
                    docs = lifecycle.snapshot(case.path,h)['documents']
                    with closing(wr.read_connection(case.path)) as db:
                        task = orders.read(db,online)
                        self.assertEqual(db.execute('PRAGMA quick_check').fetchone(),('ok',))
                        if wh == changed:
                            self.assertEqual(orders.read(db,online),new_task)
                    evidence({'scenario':'unpaid_redo','abnormal_warehouse':changed,'warehouse':wh,
                              'refreshed':refresh,'warehouse_order':h['number'],'sign_revision':h['revision'],
                              'return_version':docs['return']['version'],
                              'operations_version':(docs.get('operations') or {}).get('version'),
                              'demand_stage':'operations' if operations else 'return',
                              'old_online':first if wh==changed else second,
                              'old_internal':old['internal'] if wh==changed else task['internal'],
                              'new_online':online if wh==changed else None,'new_internal':task['internal'],
                              'original_remaining':original_balance if wh==changed else STOCK[wh]-QUANTITY[wh],
                              'released':redo['released'] if wh==changed else {},
                              'new_reserved':after['quantity'] if wh==changed else str(QUANTITY[wh]),
                              'remaining_after_release':STOCK[wh] if wh==changed else None,
                              'remaining_final':case.remaining(gid),'allocation_gid':gid,
                              'original_gid':original[wh],'payment_version':1,
                              'purchase_order':bound['result']['binding']['orderId'],
                              'receipt_version':saved['version'],
                              'jst_submissions':sum(a=='import' and k in ({first,successor} if wh==changed else {second}) for a,k in submitted+case.jst.calls),
                              'ax_submissions':case.ax.uploads.count(package['id']),'receipt_second_write':repeat['changed']})

    def test_paid_cancel_blocks_scope_old_guard_and_queue_even_after_review(self):
        from jst_cancellation import save_review
        for changed in WAREHOUSES:
            for decision in ('reuse','correct','stop'):
                with self.subTest(changed=changed,decision=decision):
                    case = self.dual()
                    gids = case.begin()
                    online = {w:case.order(w,g) for w,g in gids.items()}
                    for key in online.values():
                        case.pay(key)
                    head = case.heads[changed]
                    old_payment, = lifecycle.history(case.path,head,'payment')
                    original = case.make_package(changed)
                    queued = case.queue(changed,original)
                    validate_for_upload(case.path,original,case.config,SUPPLIERS)
                    self.assertTrue(case.ax.guard(original)['ok'])
                    self.assertEqual(case.cancel(online[changed])['stage'],'CANCELLED')
                    pending = daily_batch.read(case.path,case.root/'stage.sqlite',
                        {'stage_view':'payment','stage_filter':'待处理','all_matched':True})
                    self.assertEqual({row['warehouse'] for row in pending['records']},set(WAREHOUSES))
                    waiting, = [row for row in pending['records'] if row.get('cancelled_payment_hold')]
                    self.assertEqual(waiting['number'],head['number'])
                    self.assertIn('付款待重新核对',waiting['note'])
                    self.assertEqual(case.ax.guard(original)['code'],'PURCHASE_BASIS_CHANGED')
                    with self.assertRaisesRegex(ValueError,'付款待重新核对'):
                        purchase.freeze_payment(case.path,head,case.config,SUPPLIERS)
                    result = case.runner().run(queued['id'])['items'][0]
                    self.assertEqual((result['state'],result['error']['code']),('REVIEW','PAYMENT_CANCEL_REVIEW'))
                    self.assertEqual(case.ax.uploads,[])
                    self.assertEqual(lifecycle.history(case.path,head,'payment'),[old_payment])
                    with closing(wr.read_connection(case.path)) as db:
                        for item in old_payment['payload']['files']:
                            self.assertTrue(db.execute('SELECT 1 FROM lifecycle_files WHERE sha=?',(item['sha'],)).fetchone())
                    if decision=='correct':
                        case.manual(changed,'payment',QUANTITY[changed],Decimal('37.12'))
                    review = save_review(case.path,head,self.case_stamp(case,changed),decision,'隔离核对员','合成财务凭据QA-1')
                    with self.assertRaisesRegex(ValueError,'终止本轮采购|重新生成采购预览'):
                        validate_for_upload(case.path,original,case.config,SUPPLIERS)
                    self.assertEqual(case.ax.guard(original)['code'],'PURCHASE_BASIS_CHANGED')
                    self.assertEqual(case.ax.uploads,[])
                    if decision=='stop':
                        with self.assertRaisesRegex(ValueError,'终止本轮采购'):
                            case.make_package(changed)
                        final_po = None
                    else:
                        package = case.make_package(changed)
                        bound = case.runner().run(case.queue(changed,package)['id'])['items'][0]
                        self.assertEqual(bound['state'],'BOUND',bound)
                        final_po = bound['result']['binding']['orderId']
                    other = next(w for w in WAREHOUSES if w != changed)
                    normal = case.make_package(other)
                    bound = case.runner().run(case.queue(other,normal)['id'])['items'][0]
                    self.assertEqual(bound['state'],'BOUND',bound)
                    self.assertEqual(len(case.ax.uploads),1 if decision=='stop' else 2)
                    self.assertEqual(lifecycle.history(case.path,head,'payment')[-1],old_payment)
                    evidence({'scenario':'paid_cancel_'+decision,'abnormal_warehouse':changed,'warehouse':changed,
                              'warehouse_order':head['number'],'sign_revision':head['revision'],'return_version':1,
                              'old_online':online[changed],'old_internal':case.jst.orders[online[changed]]['internal'],
                              'payment_version':review['payment_version'],'payment_digest':review['payment_digest'],
                              'review_id':review['id'],'reviewer':review['reviewer'],'execution_online':review['execution_online'],
                              'original_remaining':case.remaining(gids[changed]),'purchase_order':final_po,
                              'jst_submissions':1,'old_ax_submissions':0,'ax_submissions':int(bool(final_po))})
                    evidence({'scenario':'paid_cancel_'+decision,'abnormal_warehouse':changed,'warehouse':other,
                              'warehouse_order':case.heads[other]['number'],'sign_revision':case.heads[other]['revision'],
                              'return_version':1,'old_online':online[other],'old_internal':case.jst.orders[online[other]]['internal'],
                              'payment_version':1,'original_remaining':case.remaining(gids[other]),
                              'purchase_order':bound['result']['binding']['orderId'],'jst_submissions':1,'ax_submissions':1})

    def test_ax_rebuild_uses_original_receipt_and_verified_void_on_both_warehouses(self):
        for changed in WAREHOUSES:
            for void in (False,True):
                with self.subTest(warehouse=changed,void=void):
                    case = self.dual()
                    gids = case.begin()
                    online = {w:case.order(w,g) for w,g in gids.items()}
                    for key in online.values():
                        case.pay(key)
                    head = case.heads[changed]
                    package = case.make_package(changed)
                    queued = case.queue(changed,package)
                    pid = package['id']
                    binding = {'orderId':'PO-SYNTH-1'}
                    file = case.workflow.folder/'component-state/state.sqlite'
                    file.parent.mkdir()
                    with closing(sqlite3.connect(file)) as db,db:
                        db.execute('CREATE TABLE purchase_creates(id TEXT PRIMARY KEY,state TEXT,platform_order TEXT,payload TEXT)')
                        db.execute('CREATE TABLE purchase_receipts(id TEXT PRIMARY KEY,payload TEXT)')

                    def saved_native(state, receipt=None, linked=None):
                        payload = {'id':pid,'state':state,'intent':package['intent'],'uploadAttempted':True,
                                   'receipt':receipt,'binding':linked}
                        with closing(sqlite3.connect(file)) as db,db:
                            db.execute('INSERT OR REPLACE INTO purchase_creates VALUES(?,?,?,?)',
                                       (pid,state,(linked or {}).get('orderId'),json.dumps(payload)))

                    def refuse():
                        original = case.workflow.get(queued['id'])
                        before = case.workflow.list()
                        with self.assertRaisesRegex(ValueError,'继续核对原采购'):
                            case.workflow.rebuild_selected(queued['id'],[head['number']],original['sequence'],[head])
                        self.assertEqual(case.workflow.list(),before)

                    validate_for_upload(case.path,package,case.config,SUPPLIERS)
                    case.ax.request('purchase.submit',purchase_id=pid)
                    # The controlled request consumed the real dispatch guard.
                    # Simulate losing its receipt, then receiving it on the original.
                    saved_native('NEEDS_REVIEW')
                    case.workflow.update(queued['id'],state='COMPLETED_WITH_ERRORS',position=0,
                                         changes={'state':'REVIEW','submission_started':True,
                                                  'error':{'code':'SUBMISSION_UNKNOWN'}})
                    refuse()
                    task_id = case.ax.records[pid]['taskId']
                    saved_native('ACCEPTED',{'taskId':task_id})
                    refuse()
                    resumed = Workflows(case.workflow.folder,business_path=case.path)
                    original = resumed.get(queued['id'])
                    resumed.recover_original(queued['id'],original['sequence'])
                    out = case.runner().run(queued['id'])['items'][0]
                    self.assertEqual((out['state'],out['result']['binding']),('BOUND',binding))
                    saved_native('BOUND',{'taskId':task_id},binding)
                    refuse()
                    self.assertEqual(case.ax.uploads,[pid])
                    if void:
                        p = package['intent']
                        receipt = {'state':'VOIDED','terminalKind':'VOIDED','terminal':True,'finalSync':'NOT_DUE',
                                   'scope':{'orderId':binding['orderId'],'store':head['store'],
                                            'supplier':p['supplierCode'],'identity':p['identity']},
                                   'basis':p['basis'],'summary':{'status':'已作废','issueCount':0}}
                        with closing(sqlite3.connect(file)) as db,db:
                            db.execute('INSERT INTO purchase_receipts VALUES(?,?)',(pid,json.dumps(receipt)))
                        old = case.workflow.get(queued['id'])
                        successor = case.workflow.rebuild_selected(old['id'],[head['number']],old['sequence'],[head])
                        self.assertEqual(case.ax.uploads,[pid])
                        alternative = case.make_package(changed)
                        bound = case.runner().run(case.queue(changed,alternative,successor)['id'])['items'][0]
                        self.assertEqual(bound['state'],'BOUND',bound)
                        saved,repeated = case.receive(alternative)
                        self.assertEqual(case.ax.uploads,[pid,alternative['id']])
                        new_number = bound['result']['binding']['orderId']
                    else:
                        def still_pending(job):
                            before = case.workflow.schedule(pid)
                            self.assertTrue(before['final_pending'])
                            refuse()
                            self.assertEqual(case.workflow.schedule(pid),before)
                            self.assertEqual(case.workflow.get(job['id']),job)
                        saved,repeated = case.receive(package,verify_pending=still_pending)
                        self.assertEqual(case.ax.uploads,[pid])
                        new_number = None
                    self.assertEqual((saved['version'],repeated['version'],repeated['changed']),(1,1,False))
                    other = next(w for w in WAREHOUSES if w!=changed)
                    follower = case.make_package(other)
                    normal = case.runner().run(case.queue(other,follower)['id'])['items'][0]
                    self.assertEqual(normal['state'],'BOUND',normal)
                    saved_other, _ = case.receive(follower)
                    for wh, new_po, num in ((changed,new_number,1+int(void)),(other,normal['result']['binding']['orderId'],1)):
                        h = case.heads[wh]
                        evidence({'scenario':'ax_verified_void' if void else 'ax_original_final_receipt',
                                  'abnormal_warehouse':changed,'warehouse':wh,'warehouse_order':h['number'],
                                  'sign_revision':h['revision'],'return_version':1,'payment_version':1,'receipt_version':1,
                                  'old_online':online[wh],'old_internal':case.jst.orders[online[wh]]['internal'],
                                  'original_task':task_id if wh==changed else case.ax.records[follower['id']]['taskId'],
                                  'old_ax_submissions':1,'ax_submissions':num,'jst_submissions':1,
                                  'purchase_order':binding['orderId'] if wh==changed else new_po,
                                  'new_purchase_order':new_po if wh==changed else None,
                                  'old_pending_retained':wh==changed and not void,'receipt_second_write':False,
                                  'allocation_gid':gids[wh],'remaining_final':case.remaining(gids[wh])})
                    self.assertEqual(saved_other['version'],1)

    def test_manual_paid_and_received_orders_cannot_be_resigned_by_either_entry(self):
        for changed in WAREHOUSES:
            for receipt in (False,True):
                with self.subTest(warehouse=changed,received=receipt):
                    case = self.dual()
                    head = case.heads[changed]
                    case.manual(changed,'return',20)
                    case.manual(changed,'payment',20,40)
                    if receipt:
                        case.manual(changed,'receipt',19,38)
                    before = lifecycle.snapshot(case.path,head)
                    self.assertEqual(before['status'],'已收齐' if receipt else '待收货')
                    self.assertEqual(case.sign(changed)['warehouse_heads'][0]['revision'],head['revision'])
                    payload = signing_fixture([('T',(changed,),36),('OTHER',('河北仓',),24)],count=2)
                    payload['execution_warehouses']={'T':[changed],'OTHER':['河北仓']}
                    target = case.root/'exports'
                    target.mkdir()
                    title = '正式实收' if receipt else '正式付款'
                    single = normal_delivery.prepare(case.repo,payload,'T')
                    with self.assertRaisesRegex(WorkFailure,title) as single_failure:
                        normal_delivery.execute(case.repo,single,target)
                    self.assertEqual(single_failure.exception.evidence['commit_state'],'not_started')
                    batch = normal_delivery.prepare_batch(case.repo,payload,['OTHER','T'])
                    with self.assertRaisesRegex(WorkFailure,title) as batch_failure:
                        normal_delivery.execute_batch(case.repo,batch,target)
                    self.assertEqual(batch_failure.exception.evidence['commit_state'],'not_started')
                    self.assertEqual(list(target.iterdir()),[])
                    self.assertNotIn('OTHER',[r['store'] for r in case.repo.headers()])
                    source = case.repo.get(head['day'],'T',head['revision'])
                    normal_delivery.export_signed(case.repo,source,target,[changed])
                    self.assertEqual(len(list(target.glob('*.xlsx'))),1)
                    case.manual(changed,'receipt' if receipt else 'payment',18,36)
                    other = next(w for w in WAREHOUSES if w!=changed)
                    case.sign(other,36)
                    current = {h['warehouse']:h for h in purchases.targets(case.path)}
                    self.assertEqual(current[changed]['revision'],head['revision'])
                    after = lifecycle.snapshot(case.path,current[changed])
                    self.assertEqual(after['status'],before['status'])
                    self.assertEqual(after['documents']['receipt' if receipt else 'payment']['version'],2)
                    for wh in WAREHOUSES:
                        h = current[wh]
                        view = lifecycle.snapshot(case.path,h)
                        evidence({'scenario':'manual_receipt_resign' if receipt else 'manual_payment_resign',
                                  'abnormal_warehouse':changed,'warehouse':wh,'warehouse_order':h['number'],
                                  'sign_revision':h['revision'],'status':view['status'],
                                  'return_version':(view['documents']['return'] or {}).get('version'),
                                  'payment_version':(view['documents']['payment'] or {}).get('version'),
                                  'receipt_version':(view['documents']['receipt'] or {}).get('version'),
                                  'jst_submissions':0,'ax_submissions':0,'old_resign_blocked':wh==changed})

    def test_finance_correction_keeps_original_purchase_final_receipt_todo_on_restart(self):
        for changed in WAREHOUSES:
            with self.subTest(warehouse=changed):
                case = self.dual()
                gids = case.begin()
                online = case.order(changed,gids[changed])
                case.pay(online)
                package = case.make_package(changed)
                original = case.queue(changed,package)
                bound = case.runner().run(original['id'])['items'][0]
                self.assertEqual(bound['state'],'BOUND',bound)
                record = {**case.ax.records[package['id']],'basis':package['intent']['basis']}
                workflow = case.workflow
                workflow.configure_schedules([record],5,True)
                queued = workflow.receipt([record])
                workflow.observe_schedule(package['id'],{'state':'READY','terminal':True,'summary':{'status':'已完成'}})
                self.assertEqual(case.cancel(online)['stage'],'CANCELLED')
                case.manual(changed,'payment',QUANTITY[changed],37.12)
                restarted = Workflows(workflow.folder,business_path=case.path)
                restarted.retire_receipt_sources()
                self.assertEqual(restarted.get(queued['id']),queued)
                self.assertTrue(restarted.schedule(package['id'])['final_pending'])
                item = case.runner().run(queued['id'])['items'][0]
                self.assertEqual((item['state'],item['error']['code']),('REVIEW','RECEIPT_PAYMENT_REVIEW'))
                self.assertTrue(restarted.schedule(package['id'])['final_pending'])
                self.assertFalse(lifecycle.history(case.path,case.heads[changed],'receipt'))
                self.assertEqual(case.ax.uploads,[package['id']])


if __name__ == '__main__':
    unittest.main()
