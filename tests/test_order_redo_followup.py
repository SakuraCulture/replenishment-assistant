"""Isolated regressions through the order, allocation and receipt entry points."""
from contextlib import closing
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import tempfile
from unittest import TestCase
from unittest.mock import patch

import aoxiang_receipts
from aoxiang_receipt_scope import assess
from aoxiang_payment_preflight import preflight_payment
from aoxiang_preview_scope import inspect_scope
import aoxiang_purchase as purchase
from aoxiang_purchase_tasks import validate_for_upload
from aoxiang_workflows import Workflows
import daily_batch
import jst_auto_order as automatic
import jst_auto_order_store as orders
import jst_batches
import jst_cancellation as cancellation
import jst_one_click
import jst_order_store as allocations
from jst_one_click_fixture import Component
import order_lifecycle as lifecycle
from test_order_redo_p1 import Dual, SUPPLIERS, STOCK, QUANTITY, WAREHOUSES, evidence
import work_repository as wr


class FollowUp(TestCase):
    def dual(self):
        root = self.enterContext(tempfile.TemporaryDirectory())
        return Dual(Path(root))

    def paid(self, case):
        gids = case.begin()
        online = {wh: case.order(wh, gid) for wh, gid in gids.items()}
        for key in online.values():
            self.assertEqual(case.pay(key)['version'], 1)
        return gids, online

    def read(self, case, online):
        with closing(wr.read_connection(case.path)) as db:
            return orders.read(db, online)

    def reopen(self, case):
        case.workflow = Workflows(case.workflow.folder, business_path=case.path)

    def fulfill(self, case, warehouse):
        package = case.make_package(warehouse)
        item = case.runner().run(case.queue(warehouse, package)['id'])['items'][0]
        self.assertEqual(item['state'], 'BOUND', item)
        saved, repeat = case.receive(package)
        self.assertEqual((saved['version'], repeat['version'], repeat['changed']), (1,1,False))
        return package['id']

    def native_binding(self, case, package):
        component = case.workflow.folder/'component-state/state.sqlite'
        component.parent.mkdir(exist_ok=True)
        record = {**case.ax.records[package['id']], 'intent':package['intent']}
        with closing(sqlite3.connect(component)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS purchase_creates(id TEXT PRIMARY KEY,state TEXT,platform_order TEXT,payload TEXT)')
            db.execute('INSERT INTO purchase_creates VALUES(?,?,?,?)',
                       (package['id'], 'BOUND', record['binding']['orderId'], json.dumps(record)))

    def capture(self, case, scenario, target, gids, keys, warehouse, *, redo=None, review=None, pid=None, **extra):
        h = case.heads[warehouse]
        online = keys[warehouse]
        new = redo['online'] if redo else None
        with closing(wr.read_connection(case.path)) as db:
            original = orders.read(db, online)
            successor = orders.read(db, new) if new else None
            active = allocations.active(db, h)
            raw = dict(db.execute('SELECT sku,quantity FROM jst_reservations WHERE gid=? ORDER BY sku',
                                  (gids[warehouse],)))
            reserved = dict(db.execute('SELECT sku,quantity FROM jst_reservations WHERE gid=? ORDER BY sku',
                                       (active,)))
            release_count, = db.execute("SELECT count(*) FROM jst_events WHERE oid=? AND warehouse=? AND action='原取消单释放占用并登记后继'",
                                        (h['oid'], warehouse)).fetchone()
            self.assertEqual(release_count, int(bool(redo)))
            self.assertEqual(db.execute('PRAGMA quick_check').fetchone(), ('ok',))
        schedule = case.workflow.schedule(pid) if pid else None
        docs = lifecycle.snapshot(case.path,h)['documents']
        account = original['proof']['binding']
        evidence({'scenario':scenario,'abnormal_warehouse':target,'warehouse':warehouse,'warehouse_order':h['number'],
                  'sign_revision':h['revision'],'return_version':docs['return']['version'],
                  'account':{k:account[k] for k in ('coid','uid','shop_id','company')},
                  'old_online':online,'old_internal':original['internal'],'old_reservation':raw,
                  'new_online':new,'new_internal':successor['internal'] if successor else None,
                  'released':redo['released'] if redo else {},'new_reserved':reserved if redo else {},
                  'active_reserved':reserved,'release_count':release_count,
                  'remaining_final':case.remaining(active or gids[warehouse]),
                  'payment_versions':[d['version'] for d in reversed(lifecycle.history(case.path,h,'payment'))],
                  'payment_review':review,'purchase_order':(schedule or {}).get('order_id'),
                  'receipt_state':(schedule or {}).get('last_state'),
                  'receipt_version':docs['receipt']['version'] if docs['receipt'] else None,
                  'jst_submissions':sum(a=='import' and k in (online,new) for a,k in case.jst.calls),
                  'ax_submissions':case.ax.uploads.count(pid),'receipt_posts':len(lifecycle.history(case.path,h,'receipt')),
                  **extra})

    def fail_lookup(self, case, failure, original):
        action = case.jst.action
        def controlled(operation, task, **kwargs):
            if operation == 'lookup' and task['online'] == original:
                if failure == 'missing':
                    return {'found': False}
                if failure == 'identity':
                    result = action(operation, task, **kwargs)
                    return {**result, 'binding': {**result['binding'], 'coid': 'other-account'}}
                raise automatic.Check('合成平台查询超时')
            return action(operation, task, **kwargs)
        return patch.object(case.jst, 'action', side_effect=controlled)

    def test_legacy_hold_survives_failed_first_lookup_and_restart(self):
        for wh in WAREHOUSES:
            for failure in ('missing', 'identity', 'timeout'):
                with self.subTest(warehouse=wh, failure=failure):
                    case = self.dual()
                    gids, keys = self.paid(case)
                    head, online = case.heads[wh], keys[wh]
                    original, = lifecycle.history(case.path, head, 'payment')
                    package = case.make_package(wh)
                    validate_for_upload(case.path, package, case.config, SUPPLIERS)
                    queued = case.queue(wh, package)
                    # The rc13 schema stored the observation here, before the
                    # first repair introduced a separate verified cancellation.
                    with closing(case.work.connect()) as db, db:
                        db.execute("UPDATE jst_auto_orders SET stage='REVIEW',reason='聚水潭状态为已取消' WHERE online=?", (online,))
                    with self.assertRaisesRegex(ValueError, '付款待重新核对'):
                        preflight_payment(case.path, head)
                    with self.fail_lookup(case, failure, online):
                        result = case.recover(online)
                    failed_reason = result['reason']
                    self.reopen(case)
                    self.assertNotIn('聚水潭状态为已取消', result['reason'])
                    self.assertNotIn('聚水潭状态为已取消', self.read(case, online)['reason'])
                    with self.assertRaisesRegex(ValueError, '付款待重新核对'):
                        preflight_payment(case.path, head)
                    with self.assertRaisesRegex(ValueError, '付款待重新核对'):
                        purchase.freeze_payment(case.path, head, case.config, SUPPLIERS)
                    self.assertEqual(case.ax.guard(package,consume=True)['code'], 'PURCHASE_BASIS_CHANGED')
                    with closing(wr.read_connection(case.path)) as db:
                        self.assertEqual(db.execute('SELECT state FROM purchase_dispatch_guards WHERE purchase_id=?',
                                                    (package['id'],)).fetchone(), ('AUTHORIZED',))
                    other = next(w for w in WAREHOUSES if w != wh)
                    selection = inspect_scope(case.path, case.root/'no-original-purchase',
                                              [head, case.heads[other]])
                    self.assertEqual([r['state'] for r in selection], ['blocked','ready'], selection)
                    self.assertIn('付款待重新核对', selection[0]['message'])
                    selection = inspect_scope(case.path, case.workflow.folder, [head,case.heads[other]])
                    self.assertEqual([r['state'] for r in selection], ['original','ready'], selection)
                    result = case.runner().run(queued['id'])['items'][0]
                    self.assertEqual(result['error']['code'], 'PAYMENT_CANCEL_REVIEW')
                    self.assertFalse(case.ax.uploads)
                    with self.assertRaisesRegex(ValueError, '已取消'):
                        cancellation.begin_redo(case.path, online)
                    self.assertEqual(case.remaining(gids[wh]), STOCK[wh]-QUANTITY[wh])
                    waiting = daily_batch.read(case.path, case.root/'stage.sqlite', {
                        'stage_view':'payment', 'stage_filter':'待处理', 'all_matched':True})['records']
                    self.assertEqual([r['number'] for r in waiting], [head['number']])
                    self.assertIn('付款待重新核对', waiting[0]['note'])
                    self.assertEqual(case.cancel(online)['stage'], 'CANCELLED')
                    with self.assertRaisesRegex(ValueError, '付款待重新核对'):
                        preflight_payment(case.path, head)
                    self.assertEqual(lifecycle.history(case.path, head, 'payment'), [original])
                    with closing(wr.read_connection(case.path)) as db:
                        self.assertEqual(db.execute('SELECT internal FROM jst_cancellation_holds WHERE online=?',(online,)).fetchone(),
                                         (self.read(case,online)['internal'],))
                        self.assertTrue(all(db.execute('SELECT 1 FROM lifecycle_files WHERE sha=?',(f['sha'],)).fetchone()
                                            for f in original['payload']['files']))
                    normal = self.fulfill(case, other)
                    self.capture(case,'legacy_'+failure,wh,gids,keys,wh,lookup_issue=failed_reason,
                                 pre_review_uploads=0)
                    self.capture(case,'legacy_'+failure,wh,gids,keys,other,pid=normal)

    def one_click(self, case, head, *, disconnect=False):
        plan = jst_one_click.preflight(case.path, [head])
        self.assertEqual(len(plan['ready']), 1, plan)
        entry, = plan['ready']
        component = Component(case.root/'component')
        if entry['mode'] != 'recover':
            b = entry['binding']
            component.accounts = [{'warehouse':head['warehouse'], 'expectedCompany':b['company'],
                                   'expectedCoid':b['coid'], 'expectedUid':b['uid'],
                                   'bindingVersion':b['version'], 'mappingVerified':True, 'enabled':True}]
        def refresh(path, lease, wh, binding, progress, cancel):
            value = case.stock(wh, STOCK[wh])
            return {'revision':value['revision'], 'source_sha256':value['source_sha256']}
        case.jst.disconnected = disconnect
        try:
            with patch.object(automatic, 'Browser', return_value=case.jst), patch.object(jst_one_click, '_refresh', side_effect=refresh) as sync:
                result, = jst_one_click.execute(case.path, [entry], component)
            self.assertEqual(sync.call_count, int(entry['mode']=='allocate'))
            return result, entry['mode']
        finally:
            case.jst.disconnected = False

    def test_paid_redo_runs_one_click_before_financial_review(self):
        for wh in WAREHOUSES:
            for decision in ('reuse', 'correct', 'stop'):
                with self.subTest(warehouse=wh, decision=decision):
                    case = self.dual()
                    gids, keys = self.paid(case)
                    other = next(w for w in WAREHOUSES if w != wh)
                    head, online = case.heads[wh], keys[wh]
                    old, = lifecycle.history(case.path, head, 'payment')
                    previous = case.make_package(wh)
                    validate_for_upload(case.path, previous, case.config, SUPPLIERS)
                    self.assertEqual(case.cancel(online)['stage'], 'CANCELLED')
                    frozen_old = self.read(case,online)
                    redo = cancellation.begin_redo(case.path, online)
                    self.assertEqual(redo['released'], {'00000':str(QUANTITY[wh])})
                    self.assertEqual(cancellation.begin_redo(case.path, online), redo)
                    self.assertEqual(case.remaining(gids[wh]), STOCK[wh])
                    quantity = 12 if decision == 'correct' else QUANTITY[wh]
                    if decision == 'correct':
                        case.manual(wh, 'return', quantity)
                    first, mode = self.one_click(case, head, disconnect=True)
                    self.assertEqual((mode, first['status'], first.get('online')),
                                     ('allocate', '导入结果待核', redo['online']), first)
                    self.reopen(case)
                    recovered, mode = self.one_click(case, head)
                    self.assertEqual((mode, recovered['online']), ('recover', redo['online']))
                    successor = self.read(case, redo['online'])
                    self.assertEqual(successor['stage'], 'IMPORTED')
                    self.assertEqual(self.read(case, online)['stage'], 'CANCELLED')
                    self.assertEqual(case.remaining(successor['gid']), STOCK[wh]-quantity)
                    self.assertEqual(case.remaining(gids[other]), STOCK[other]-QUANTITY[other])
                    # A lookup already in progress for the retired original
                    # delivers its result after the successor was registered.
                    late = case.jst.action('lookup', frozen_old, identity_only=True)
                    self.assertTrue(automatic._record_found(case.path,frozen_old,late,None,identity_only=True)['historical'])
                    self.assertEqual(self.read(case,redo['online']),successor)
                    with self.assertRaisesRegex(ValueError, '付款待重新核对'):
                        preflight_payment(case.path, head)
                    self.assertFalse(case.ax.uploads)
                    self.assertEqual(case.ax.guard(previous,consume=True)['code'], 'PURCHASE_BASIS_CHANGED')
                    self.assertEqual(lifecycle.history(case.path, head, 'payment'), [old])
                    if decision == 'correct':
                        self.assertEqual(case.pay(redo['online'])['version'], 2)
                    with closing(wr.read_connection(case.path)) as db:
                        old_rows = dict(db.execute('SELECT sku,quantity FROM jst_reservations WHERE gid=?',(gids[wh],)))
                        new_rows = dict(db.execute('SELECT sku,quantity FROM jst_reservations WHERE gid=?',(successor['gid'],)))
                        self.assertEqual(old_rows, redo['released'])
                        self.assertEqual(new_rows, {'00000':str(quantity)})
                        stamp = cancellation.review_context(db, head)['stamp']
                    approved = cancellation.save_review(case.path, head, stamp, decision, '隔离核对员', '合成财务依据')
                    self.assertEqual(case.ax.guard(previous,consume=True)['code'], 'PURCHASE_BASIS_CHANGED')
                    pid = None
                    if decision == 'stop':
                        with self.assertRaisesRegex(ValueError, '终止本轮采购'):
                            preflight_payment(case.path, head)
                    else:
                        self.assertEqual(preflight_payment(case.path, head)['payment']['version'], approved['payment_version'])
                        pid = self.fulfill(case,wh)
                    self.assertEqual(sum(a=='import' and k in (online, redo['online']) for a,k in case.jst.calls), 2)
                    normal = self.fulfill(case,other)
                    self.capture(case,'redo_'+decision,wh,gids,keys,wh,redo=redo,review=approved,pid=pid,
                                 remaining_after_release=STOCK[wh],pre_review_uploads=0)
                    self.capture(case,'redo_'+decision,wh,gids,keys,other,pid=normal)

    def test_amount_only_correction_keeps_original_receipt_work(self):
        for wh in WAREHOUSES:
            for completed in (False, True):
                with self.subTest(warehouse=wh, completed=completed):
                    case = self.dual()
                    gids, keys = self.paid(case)
                    h = case.heads[wh]
                    package = case.make_package(wh)
                    pid = package['id']
                    queued = case.queue(wh,package)
                    bound = case.runner().run(queued['id'])['items'][0]
                    self.assertEqual(bound['state'], 'BOUND')
                    self.native_binding(case,package)
                    record, proof = case.stage_receipt(package)
                    final = dict(case.ax.observations[pid])
                    if not completed:
                        case.ax.observations[pid] = {**final, 'state':'IDLE','terminal':False,
                                                    'terminalKind':None,'summary':{'status':'采购中'},'manifest':None}
                    case.workflow.configure_schedules([record], 5, True)
                    job = case.workflow.receipt([record])
                    case.workflow.observe_schedule(pid, case.ax.observations[pid])
                    old = lifecycle.history(case.path, h, 'payment')[0]
                    new, = case.manual(wh, 'payment', QUANTITY[wh],
                                       Decimal(package['intent']['totalAmount'])+Decimal('1.23'), include_zero=False)
                    self.assertEqual(new['version'], 2)
                    with closing(wr.read_connection(case.path)) as db:
                        self.assertIsNone(cancellation.read(db, h))
                    case.workflow.retire_receipt_sources()
                    self.reopen(case)
                    schedule = case.workflow.schedule(pid)
                    self.assertFalse(schedule.get('retired'), schedule)
                    self.assertEqual((schedule['basis'], schedule['order_id'], schedule['final_pending']),
                                     (record['basis'], record['binding']['orderId'], completed))
                    self.assertNotIn(case.workflow.get(job['id'])['items'][0]['state'], ('SKIPPED','CANCELLED','SYNCED'))
                    self.assertTrue(schedule['needs_review'])
                    self.assertIsNotNone(case.workflow.get(queued['id'])['items'][0]['result']['binding'])
                    before = case.workflow.get(queued['id'])
                    with self.assertRaisesRegex(ValueError,'继续核对原采购'):
                        case.workflow.rebuild_selected(before['id'],[h['number']],before['sequence'],[h])
                    self.assertEqual(case.workflow.get(queued['id']),before)
                    display = daily_batch.read(case.path,case.root/'stage.sqlite',
                                               {'stage_view':'receipt','stage_filter':'待处理','platform_tracking':True})
                    source, = [r['platform_purchase'] for r in display['records'] if r['number']==h['number']]
                    self.assertEqual((source['purchase_id'],source['orderId'],source['payment_review']),
                                     (pid,record['binding']['orderId'],True))
                    if not completed:
                        blocked_early = case.runner().run(job['id'])
                        self.assertEqual(blocked_early['items'][0]['error']['code'], 'RECEIPT_PAYMENT_REVIEW')
                        self.assertTrue(case.workflow.schedule(pid)['enabled'])
                        self.assertFalse(case.workflow.schedule(pid)['final_pending'])
                        self.assertFalse(lifecycle.history(case.path,h,'receipt'))
                        case.ax.observations[pid] = final
                        case.workflow.retry_items(job['id'],[0],blocked_early['sequence'])
                    blocked = case.runner().run(job['id'])['items'][0]
                    self.assertEqual((blocked['state'],blocked['error']['code']),('REVIEW','RECEIPT_PAYMENT_REVIEW'),blocked)
                    with self.assertRaisesRegex(ValueError, '付款|人工核对'):
                        aoxiang_receipts.synchronize(case.path, package, proof, case.config, SUPPLIERS)
                    self.assertFalse(lifecycle.history(case.path, h, 'receipt'))
                    self.reopen(case)
                    change = assess(case.path,[{'key':pid,'basis':record['basis']}])[pid]['payment_review']
                    from aoxiang_receipt_scope import review_stamp
                    self.assertEqual(Decimal(change['difference']),Decimal('1.23'))
                    self.assertEqual([(r[0],Decimal(r[1])) for r in change['rows']],[('00000',Decimal(QUANTITY[wh]))])
                    approval = {'stamp':review_stamp(change,pid,record['binding']['orderId'],proof['sha256']),
                                'reviewer':'隔离收货员','basis':'合成原采购实收及付款差额凭据'}
                    stale = {**approval,'stamp':{**approval['stamp'],'order_id':'other-order'}}
                    with self.assertRaisesRegex(ValueError,'已变化'):
                        aoxiang_receipts.synchronize(case.path,package,proof,case.config,SUPPLIERS,approval=stale)
                    saved = aoxiang_receipts.synchronize(case.path,package,proof,case.config,SUPPLIERS,approval=approval)
                    self.assertTrue(saved['changed'])
                    # Commit succeeded, but acknowledge only on the restarted
                    # original task. Same native proof may be safely replayed.
                    self.reopen(case)
                    same = aoxiang_receipts.synchronize(case.path,package,proof,case.config,SUPPLIERS)
                    self.assertEqual((saved['version'],same['version'],same['changed']),(1,1,False))
                    status = case.ax.request('receipt.synced',purchase_id=pid,receipt=same,manifest_sha256=proof['sha256'])
                    case.workflow.observe_schedule(pid,status)
                    self.assertEqual(case.workflow.get(job['id'])['items'][0]['state'],'SYNCED')
                    schedule = case.workflow.schedule(pid)
                    self.assertEqual((schedule['last_state'],schedule['needs_review'],schedule['final_pending']),('SYNCED',False,False))
                    receipt, = lifecycle.history(case.path,h,'receipt')
                    audit = receipt['payload']['aoxiang_payment_review']
                    self.assertEqual((audit['order_id'], audit['original']['version'], audit['current']['version']),
                                     (record['binding']['orderId'],1,2))
                    self.assertEqual(receipt['payload']['basis']['dependencies']['payment'],audit['current'])
                    self.assertEqual(sum(Decimal(r['amount']) for r in receipt['payload']['rows']),
                                     Decimal(package['intent']['totalAmount']))
                    self.assertEqual(lifecycle.history(case.path,h,'payment')[-1], old)
                    self.assertEqual(case.ax.uploads, [pid])
                    other = next(w for w in WAREHOUSES if w != wh)
                    normal = self.fulfill(case,other)
                    name = 'amount_completed' if completed else 'amount_in_progress'
                    self.capture(case,name,wh,gids,keys,wh,review=audit,pid=pid,receipt_repeat_same_version=True)
                    self.capture(case,name,wh,gids,keys,other,pid=normal)

    def test_same_receipt_already_posted_before_repricing_cannot_post_again(self):
        from aoxiang_receipt_scope import review_stamp
        case = self.dual()
        self.paid(case)
        wh = '河北仓'
        h = case.heads[wh]
        package = case.make_package(wh)
        bound = case.runner().run(case.queue(wh,package)['id'])['items'][0]
        self.assertEqual(bound['state'],'BOUND')
        record, proof = case.stage_receipt(package)
        aoxiang_receipts.synchronize(case.path,package,proof,case.config,SUPPLIERS)
        # The local receipt was saved, but the component acknowledgement was lost.
        old, = lifecycle.history(case.path,h,'receipt')
        case.manual(wh,'payment',QUANTITY[wh],Decimal('41.23'),include_zero=False)
        pid = package['id']
        change = assess(case.path,[{'key':pid,'basis':record['basis']}])[pid]['payment_review']
        approval = {'stamp':review_stamp(change,pid,record['binding']['orderId'],proof['sha256']),
                    'reviewer':'隔离收货员','basis':'检查丢失回执后同一原件'}
        with self.assertRaisesRegex(ValueError,'已入账'):
            aoxiang_receipts.synchronize(case.path,package,proof,case.config,SUPPLIERS,approval=approval)
        self.assertEqual(lifecycle.history(case.path,h,'receipt'),[old])
        self.assertEqual(case.ax.uploads,[pid])
