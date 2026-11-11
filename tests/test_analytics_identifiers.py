"""Frozen ID correspondence, workbook compatibility, and read-only local source guards."""
import copy
import io
import json
import sqlite3
import threading
import unittest
from contextlib import closing
from unittest.mock import patch

import openpyxl
import test_order_analytics as fixtures
import order_analytics as a
import order_analytics_exports as ex
import analytics_identifiers as ids
import payment_references as refs
import order_lifecycle as lc
from runtime_support import Cancelled


def seed_purchase(path,head,payment,id='purchase-one',number='PO00001',basis_changes=None,state='BOUND'):
    file=path.parent/'aoxiang/component-state/state.sqlite';file.parent.mkdir(parents=True,exist_ok=True)
    basis={'oid':head['oid'],'revision':head['revision'],'signedDigest':head['digest'],'number':head['number'],
           'store':head['store'],'warehouse':head['warehouse'],'paymentVersion':payment['version'],'paymentDigest':payment['digest']}
    basis.update(basis_changes or {})
    record={'id':id,'state':state,'intent':{'basis':basis,'identity':{'accountId':'synthetic','merchantCode':'offline'}},
            'binding':{'orderId':number,'status':'待提交','verifiedAt':'2026-11-08T12:00:00+08:00'}}
    with closing(sqlite3.connect(file)) as db,db:
        db.execute('CREATE TABLE IF NOT EXISTS purchase_creates(id TEXT PRIMARY KEY,order_key TEXT NOT NULL,identity_key TEXT NOT NULL,request_hash TEXT NOT NULL,state TEXT NOT NULL,sequence INTEGER NOT NULL,platform_order TEXT,payload TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS purchase_receipts(id TEXT PRIMARY KEY,payload TEXT NOT NULL)')
        db.execute('INSERT INTO purchase_creates VALUES(?,?,?,?,?,?,?,?)',(id,'key-'+id,'identity','hash',state,1,number,json.dumps(record,ensure_ascii=False)))
    return file,record


class IdentifierTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.AnalyticsTests();self.f.setUp();self.path=self.f.path
        self.f.f.save('return');self.f.f.save('payment');self.report=self.f.report()
        self.head=next(h for h in self.report['heads'] if h['number']==self.f.f.h['number'])
        self.payment=lc.snapshot(self.path,self.head)['documents']['payment']

    def tearDown(self):self.f.tearDown()

    def entry(self,report=None):
        return next(r['references'] for r in (report or self.f.report())['identifiers'] if r['order']==self.head['number'])

    def test_native_payment_number_and_manual_override_are_frozen_together(self):
        self.assertEqual(self.entry()['jst_numbers'],['D001'])
        refs.save(self.path,refs.read(self.path,self.head),'000994021108001')
        report=self.f.report(); self.assertEqual(self.entry(report)['jst_numbers'],['000994021108001'])
        self.assertEqual(self.entry(report)['signed_number'],self.head['number'])
        self.assertEqual(self.entry(self.report)['jst_numbers'],['D001'])
        self.assertNotEqual(report['snapshot_id'],self.report['snapshot_id'])
        self.assertEqual(report['metrics'],self.report['metrics'])

    def test_bound_purchase_ids_are_read_without_starting_a_client_or_writing_databases(self):
        file,_=seed_purchase(self.path,self.head,self.payment); before=self.path.read_bytes(),file.read_bytes()
        with patch('aoxiang_client.ProcessClient.start',side_effect=AssertionError('No platform')):
            report=self.f.report(); self.assertEqual(self.entry(report)['purchases'][0]['number'],'PO00001')
        self.assertEqual((self.path.read_bytes(),file.read_bytes()),before)
        self.assertEqual(report['metrics'],self.report['metrics'])

    def test_other_warehouse_recreated_identity_revision_and_digest_are_not_guessed(self):
        for i,change in enumerate(({'warehouse':'山东仓'},{'oid':'same-number-new-order'},{'revision':999},{'signedDigest':'0'*64})):
            seed_purchase(self.path,self.head,self.payment,id='wrong-'+str(i),number='PO-WRONG-'+str(i),basis_changes=change)
        self.assertEqual(self.entry()['purchases'],[])

    def test_missing_sources_and_accepted_task_without_order_are_not_invented(self):
        self.assertFalse((self.path.parent/'aoxiang').exists()); report=self.f.report()
        self.assertFalse((self.path.parent/'aoxiang').exists()); self.assertEqual(self.entry(report)['purchases'],[])
        file,record=seed_purchase(self.path,self.head,self.payment)
        with closing(sqlite3.connect(file)) as db,db:db.execute('UPDATE purchase_creates SET state=?,platform_order=NULL,payload=?',('ACCEPTED',json.dumps({**record,'binding':None})))
        self.assertEqual(self.entry()['purchases'],[])

    def test_multiple_and_voided_purchase_ids_remain_traceable(self):
        file,_=seed_purchase(self.path,self.head,self.payment)
        seed_purchase(self.path,self.head,self.payment,id='purchase-two',number='PO00002')
        with closing(sqlite3.connect(file)) as db,db:db.execute('INSERT INTO purchase_receipts VALUES(?,?)',('purchase-one',json.dumps({'state':'VOIDED','summary':{'status':'已作废'}})))
        report=self.f.report(); records=self.entry(report)['purchases']
        self.assertEqual([p['number'] for p in records],['PO00001','PO00002']); self.assertEqual(records[0]['status'],'已作废')
        rows=list(ids.link_rows(report['rows'])); self.assertEqual({r[6] for r in rows if r[4]==self.head['number']},{'PO00001','PO00002'})
        self.assertEqual(report['metrics'],self.report['metrics'])

    def test_old_payment_version_keeps_id_with_explicit_review_note(self):
        seed_purchase(self.path,self.head,self.payment,basis_changes={'paymentDigest':'0'*64})
        record=self.entry()['purchases'][0]; self.assertEqual(record['number'],'PO00001'); self.assertIn('付款已变化',record['note'])

    def test_legacy_frozen_package_required_and_hash_checked(self):
        from aoxiang_purchase import fingerprint
        file,record=seed_purchase(self.path,self.head,self.payment)
        record['intent']['basis'].pop('oid'); record['intent']['basis'].pop('revision'); record['intent']['basis'].pop('signedDigest')
        plan={'head':self.head,'payment':self.payment}; plan['fingerprint']=fingerprint(plan); record['intent']['basis']['planFingerprint']=plan['fingerprint']
        with closing(sqlite3.connect(file)) as db,db:db.execute('UPDATE purchase_creates SET payload=?',(json.dumps(record),))
        self.assertEqual(self.entry()['purchases'],[])
        proof=self.path.parent/'aoxiang/purchase-packages/purchase-one/付款采购依据.json';proof.parent.mkdir(parents=True);proof.write_text(json.dumps(plan,ensure_ascii=False),'utf-8')
        self.assertEqual(self.entry()['purchases'][0]['number'],'PO00001')
        plan['head']['store']='WRONG'; proof.write_text(json.dumps(plan),'utf-8')
        with self.assertRaisesRegex(ValueError,'摘要不符'):self.f.report()

    def test_inconsistent_stored_binding_stops_query(self):
        file,record=seed_purchase(self.path,self.head,self.payment);record['binding']['orderId']='OTHER'
        with closing(sqlite3.connect(file)) as db,db:db.execute('UPDATE purchase_creates SET payload=?',(json.dumps(record),))
        with self.assertRaisesRegex(ValueError,'关联凭证不符'):self.f.report()

    def test_id_change_invalidates_ui_basis_and_prevents_stale_export(self):
        refs.save(self.path,refs.read(self.path,self.head),'NEW-ID')
        with self.assertRaisesRegex(ValueError,'单号已更新'):a.validate_basis(self.path,self.report)
        target=self.f.root/'old.xlsx'
        with self.assertRaisesRegex(ValueError,'变化'):ex.export(self.path,self.report,target)
        self.assertFalse(target.exists())

    def test_purchase_change_and_status_change_invalidate_snapshot(self):
        file,_=seed_purchase(self.path,self.head,self.payment); report=self.f.report()
        with closing(sqlite3.connect(file)) as db,db:db.execute('INSERT INTO purchase_receipts VALUES(?,?)',('purchase-one',json.dumps({'state':'VOIDED','summary':{'status':'已作废'}})))
        with self.assertRaisesRegex(ValueError,'单号已更新'):a.validate_basis(self.path,report)
        with self.assertRaisesRegex(ValueError,'变化'):a.validate_snapshot(self.path,report)

    def test_excel_appends_ids_keeps_first42_cells_and_types_and_saves_exact_mapping(self):
        from funnel_compact_export import write_summary
        from xlsx_exports import stream_book
        refs.save(self.path,refs.read(self.path,self.head),'000994021108001'); seed_purchase(self.path,self.head,self.payment)
        report=self.f.report(); target=self.f.root/'with-ids.xlsx'; before=self.path.read_bytes()
        ex.export(self.path,report,target,level='order')
        stream=io.BytesIO()
        with stream_book() as book:write_summary(book,report['rows'],'order',coverage=True);book.save(stream)
        old=openpyxl.load_workbook(io.BytesIO(stream.getvalue())); new=openpyxl.load_workbook(target)
        try:
            for name in ('订单五段漏斗',):
                ws=new[name]; self.assertEqual(ws.max_column,45);self.assertEqual(list(next(ws.values))[-3:],ids.HEADERS)
                for arow,brow in zip(old[name],ws):
                    self.assertEqual([(c.value,c.data_type,c.number_format) for c in arow],[(c.value,c.data_type,c.number_format) for c in list(brow)[:42]])
            sheet=new['单号关联明细']; self.assertEqual(list(next(sheet.values)),ids.LINK_HEADERS)
            row=next(r for r in list(sheet)[1:] if r[4].value==self.head['number'])
            self.assertEqual([r.value for r in row][4:7],[self.head['number'],'000994021108001','PO00001'])
            self.assertEqual([r.data_type for r in row][4:7],['s','s','s'])
            self.assertTrue(all(c.data_type not in ('f','e') for ws in new for row in ws for c in row))
        finally:old.close();new.close()
        self.assertEqual(self.path.read_bytes(),before)

    def test_selected_export_and_group_scope_keep_only_the_selected_correspondence(self):
        seed_purchase(self.path,self.head,self.payment); report=self.f.report(); target=self.f.root/'selected.xlsx'
        ex.export(self.path,report,target,orders=[self.head['number']])
        b=openpyxl.load_workbook(target)
        try:
            self.assertEqual({r[4] for r in list(b['单号关联明细'].values)[1:]},{self.head['number']})
            from funnel_dimension_export import NAMES
            for name in NAMES:
                records=list(b[name].values)[1:]
                self.assertEqual({r[-3] for r in records},{self.head['number']} if records else set())
                if not records:self.assertEqual(name,'已剔除SKU明细')
        finally:b.close()

    def test_id_changes_during_export_prevent_publication_and_retry_uses_new_values(self):
        report=self.f.report(); target=self.f.root/'changed.xlsx'; changed=[]
        def progress(_):
            if not changed and str(_).startswith('已生成：'):
                changed.append(True);refs.save(self.path,refs.read(self.path,self.head),'NEW')
        with self.assertRaisesRegex(ValueError,'变化'):ex.export(self.path,report,target,progress=progress)
        self.assertFalse(target.exists());ex.export(self.path,self.f.report(),target)

    def test_same_order_repeated_products_deduplicate_and_multi_date_lines_stay_aligned(self):
        original=copy.deepcopy(self.report['rows'][0]); other=copy.deepcopy(original)
        original['references']['jst_numbers']=['J1'];original['references']['purchases']=[{'number':'PO1'}]
        other.update(order='PREVIOUS-DAY',day='2026-11-07');other['references'].update(signed_number='PREVIOUS-DAY',jst_numbers=[],purchases=[{'number':'PO2'}])
        value=ids.cells([original,copy.deepcopy(original),other])
        self.assertEqual(value,[self.head['number']+'\nPREVIOUS-DAY','J1\n未提供','PO1\nPO2'])

    def test_large_aggregate_retains_full_ids_in_detail_instead_of_excel_truncation(self):
        original=self.report['rows'][0]; rows=[]
        for i in range(1200):
            row={**original,'order':'ORDER-'+str(i)+'-'+'x'*30,'references':{'jst_numbers':['J'+str(i)],'purchases':[]}};rows.append(row)
        self.assertTrue(all('单号关联明细' in v for v in ids.cells(rows)))
        self.assertEqual(len(list(ids.link_rows(rows))),1200)

    def test_cancelled_id_read_and_export_leave_no_output(self):
        event=threading.Event(); event.set(); target=self.f.root/'cancelled.xlsx'
        with self.assertRaises(Cancelled):a.collect(self.path,self.f.spec,cancel=event)
        with self.assertRaises(Cancelled):ex.export(self.path,self.report,target,cancel=event)
        self.assertFalse(target.exists())


if __name__=='__main__':unittest.main()
