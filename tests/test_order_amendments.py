from contextlib import closing
from unittest import TestCase

import test_lifecycle
import order_amendments as amendments
import order_lifecycle as lifecycle
import work_repository as wr


class Corrections(TestCase):
    def setUp(self):
        self.fixture = test_lifecycle.LifecycleTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.path = self.fixture.path
        self.head, self.sibling = self.fixture.heads

    def preview(self, stage, changes, evidence=None):
        snapshot = amendments.read(self.path, self.head, stage)
        return amendments.prepare(self.path, self.head, stage, snapshot['token'], changes, '经办人甲', '按实际单据更正', evidence)

    def test_each_document_stage_keeps_original_and_replays_exact_save(self):
        source=self.fixture.root/'凭据.txt';source.write_text('隔离凭据',encoding='utf-8')
        self.fixture.h=self.sibling
        self.fixture.save('return',[{'供货仓':'山东仓','商品编码':'0002','门店确认数量':7}])
        sibling=lifecycle.history(self.path,self.sibling,'return')
        self.fixture.h=self.head
        for stage in ('return','operations','payment','receipt'):
            with self.subTest(stage=stage):
                self.fixture.save(stage,[{}, {'商品编码':'@DROP','商品名称':'待移除'}, {'商品编码':'00000','商品名称':'保留零'}])
                original=lifecycle.history(self.path,self.head,stage)[0]
                change=[{'sku':'0001','action':'set','quantity':'0','amount':'0' if stage=='payment' else None},
                        {'sku':'@DROP','action':'delete'},
                        {'sku':'0012345678901234567','action':'set','name':'长码','quantity':'2','amount':'2.50' if stage=='payment' else None},
                        {'sku':'@0002','action':'set','name':'明确空白','quantity':'','amount':'0' if stage=='payment' else None}]
                pending=self.preview(stage,change,source)
                first=lifecycle.commit(self.path,pending['write'])
                self.assertEqual(lifecycle.commit(self.path,pending['write']),first)
                saved, previous=lifecycle.history(self.path,self.head,stage)
                self.assertEqual(previous,original)
                self.assertEqual(saved['version'],2)
                self.assertEqual([r['sku'] for r in saved['payload']['raw_rows']],
                                 ['0001','00000','0012345678901234567','@0002'])
                self.assertEqual([r['base_quantity'] for r in saved['payload']['raw_rows']][2:],['2',None])
                self.assertEqual(saved['payload']['manual_correction']['changes'],pending['changes'])
                self.assertEqual(lifecycle.history(self.path,self.sibling,'return'),sibling)
                with closing(wr.read_connection(self.path)) as db:
                    self.assertEqual(db.execute("SELECT count(*) FROM lifecycle_events WHERE stage=? AND action='手动更正'",(stage,)).fetchone()[0],1)

    def test_changes_from_stale_preview_do_not_overwrite_and_payment_requires_actual_amount(self):
        self.fixture.save('return')
        self.fixture.save('payment')
        with self.assertRaisesRegex(ValueError,'实际金额'):
            self.preview('payment',[{'sku':'0001','action':'set','quantity':'7'}])
        pending=self.preview('payment',[{'sku':'0001','action':'set','quantity':'7','amount':'84'}])
        self.fixture.save('payment',[{'付款单商品数量':8,'实际金额':96}])
        with self.assertRaisesRegex(wr.Conflict,'版本'):
            lifecycle.commit(self.path,pending['write'])
        self.assertEqual(len(lifecycle.history(self.path,self.head,'payment')),2)
        with self.assertRaisesRegex(wr.Conflict,'已确认付款'):
            amendments.read(self.path,self.head,'return')

    def test_allocation_changes_use_original_batch_and_preserve_same_sku_other_warehouse(self):
        from decimal import Decimal
        import allocation_amendments as service
        import jst_batches as batches
        import jst_orders as orders
        from test_jst import catalog
        from warehouse_catalog import revision

        self.fixture.save('return',[{'门店确认数量':20}, {'补货单号':self.sibling['number'],
            '供货仓':'山东仓','商品编码':'0001','门店确认数量':16}])
        hb,sd=catalog(stock=100),catalog('山东仓',stock=145)
        hb['items']['@001']={**hb['items']['0001'],'商品编码':'@001','出库箱规':12,'_row':3}
        hb['revision']=revision(hb['items'])
        batch=batches.execute(self.path,batches.preview_new(self.path,[self.head,self.sibling],{'河北仓':hb,'山东仓':sd}))
        self.assertFalse(batch['errors'])
        batch_id=batch['batch_id']
        with closing(wr.read_connection(self.path)) as db:
            old_gids=[orders.active(db,h) for h in (self.head,self.sibling)]
        before=[orders.get(self.path,gid) for gid in old_gids]
        orders.commit_refresh(self.path,orders.prepare_refresh(self.path,'河北仓',catalog=catalog(stock=60)))
        snap=service.read(self.path,self.head,'allocation')
        edits=[{'action':'set','sku':'0001','quantity':'12'}, {'action':'set','sku':'@001','quantity':'3'}]
        plan=service.prepare(self.path,self.head,'allocation',snap['token'],edits,'经办人甲','确认改量和新增')
        answer=service.commit(self.path,plan['write'])
        self.assertEqual(service.commit(self.path,plan['write']),answer)
        self.assertEqual([orders.get(self.path,gid) for gid in old_gids],before)
        with closing(wr.read_connection(self.path)) as db:
            self.assertEqual(batches.reservations(db,batch_id,'河北仓'),{'0001':Decimal(12),'@001':Decimal(3)})
            self.assertEqual(batches.reservations(db,batch_id,'山东仓'),{'0001':Decimal(16)})
            self.assertEqual(orders.active(db,self.sibling),old_gids[1])
            self.assertEqual(db.execute("SELECT count(*) FROM jst_events WHERE action='手动更正分配'").fetchone()[0],1)
        with self.assertRaisesRegex(wr.Conflict,'已变化'):
            service.prepare(self.path,self.head,'allocation',snap['token'],edits,'经办人甲','已更新再保存')
        snap=service.read(self.path,self.head,'allocation')
        with self.assertRaisesRegex(ValueError,'可分配 100'):
            service.prepare(self.path,self.head,'allocation',snap['token'],[{'action':'set','sku':'0001','quantity':'101'}],
                            '经办人甲','超出仓库存')

    def test_signed_revision_preserves_original_and_only_advances_selected_warehouse(self):
        import purchases
        import signing_amendments as service
        import warehouse_catalog
        import warehouse_execution as execution
        from test_jst import catalog

        old=service.read(self.path,self.head,'signed')
        sibling_before=lifecycle.full_head(self.path,self.sibling)
        c=catalog();item={**c['items']['0001'],'商品编码':'00123','商品名称':'手动追加','商品单位':'件','_row':3}
        c['items']['00123']=item;c['revision']=warehouse_catalog.revision(c['items'])
        warehouse_catalog.Repository(self.path.parent).save(c)
        plan=service.prepare(self.path,self.head,'signed',old['token'],[
            {'action':'delete','sku':'0001'},{'action':'set','sku':'00123','quantity':'5'}],
            '经办人甲','换成可供应商品')
        saved=service.commit(self.path,plan['write'])
        self.assertEqual(service.commit(self.path,plan['write']),saved)
        live={h['warehouse']:h for h in purchases.targets(self.path,current_only=True)}
        self.assertEqual(live['河北仓']['revision'],2)
        self.assertEqual(live['山东仓']['revision'],self.sibling['revision'])
        self.assertEqual(lifecycle.full_head(self.path,live['山东仓'])['products'],sibling_before['products'])
        self.assertEqual(lifecycle.full_head(self.path,live['河北仓'])['products']['00123']['quantity'],5)
        archived=[h for h in purchases.targets(self.path) if h['warehouse']=='河北仓' and h['revision']==1][0]
        old_order=execution.get_order(self.fixture.repo,archived)
        self.assertEqual([r['E'] for r in old_order['payload']['result']['rows']],['0001'])

    def test_remainder_keeps_unit_conflicts_and_unconfirmed_partial_out_of_dispatch(self):
        import platform_remainders as remainder

        original=[{'sku':'0001','quantity':'8','unit':'件'},{'sku':'@01','quantity':'6','unit':'箱'}]
        observed=[{'sku':'0001','quantity':'3','unit':'件'},{'sku':'@01','quantity':'1','unit':'件'},
                  {'sku':'0003','quantity':'1','unit':'个'}]
        delta=remainder.compare(original,observed,eligible=True,require_unit=True)
        self.assertEqual([(r['sku'],r['remainder'],r['status']) for r in delta],[
            ('0001','5','待确认未入单数量'),('@01',None,'基础单位待核'),('0003',None,'平台新增商品')])
        self.assertEqual(remainder.compare(original,observed,eligible=False,require_unit=False)[0]['status'],
                         '待核 SKU 级失败证据')
        report={'head':self.head,'stage':'payment','basis_version':1,'platform':'翱象','source':'原执行',
                'platform_order':'PO-QA','task':'隔离任务','date':wr.now(),'evidence_sha':'0'*64,'rows':delta}
        saved=remainder.save(self.path,report,'隔离经办','核对原回执')
        self.assertEqual(remainder.save(self.path,report,'隔离经办','重试同一核对'),saved)
        with closing(wr.read_connection(self.path)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM lifecycle_events WHERE action=?',(remainder.ACTION,)).fetchone()[0],1)

    def test_aoxiang_partial_remainder_is_bound_to_original_import_and_native_proof(self):
        from hashlib import sha256
        import platform_remainders as remainder

        h=self.head
        def file(name, value):
            raw=wr.dumps(value).encode();path=self.fixture.root/name;path.write_bytes(raw)
            return {'file':str(path),'sha256':sha256(raw).hexdigest()}
        basis={'oid':h['oid'],'warehouse':h['warehouse'],'revision':h['revision'],'signedDigest':h['digest'],'paymentVersion':1}
        intent={'id':'purchase-1','basis':basis,'note':'AX_CREATE:purchase-1','supplierCode':'HB-1',
                'rows':[{'sku':'0001','quantity':'8'},{'sku':'@01','quantity':'6'}]}
        original=file('intent.json',intent)
        package={'package_file':original['file'],'package_sha256':original['sha256']}
        matching={'orderId':'PO-QA','store':h['store'],'supplier':'HB-1'}
        sources=[{'source':s,**file(s+'.xlsx',{'isolated':s})} for s in ('D16','D17')]
        evidence={'main':{**matching,'marker':intent['note'],'status':'待入库'},'observedAt':wr.now(),
                  'files':sources,'rows':[{**matching,'sku':'0001','quantity':'3','unit':'件','note':intent['note']}]}
        audit={'purchaseId':intent['id'],'basis':basis,'uniqueMarkedOrders':['PO-QA'],
               'request':{'original_units':{'0001':'件','@01':'箱'}},'evidence':evidence}
        manifest=file('proof.json',audit)
        record={'id':intent['id'],'state':'BOUND','basis':basis,'taskId':'task-1',
                'binding':{'manualRevision':{'rowCount':1},'proposalId':'proposal-1','proofSha256':manifest['sha256'],'orderId':'PO-QA'},
                'bindingProposal':{'id':'proposal-1','state':'CONFIRMED','manifest':manifest},
                'importResult':{'taskId':'task-1','kind':'partial','failCount':1}}
        report=remainder.aoxiang(self.path,h,package,record)
        self.assertEqual([r['remainder'] for r in report['rows']],['5','6'])
        with self.assertRaisesRegex(wr.Conflict,'归属'):
            remainder.aoxiang(self.path,self.sibling,package,record)
        from pathlib import Path
        Path(sources[1]['file']).write_text('replaced',encoding='utf-8')
        with self.assertRaisesRegex(wr.Conflict,'原件已变化'):
            remainder.aoxiang(self.path,h,package,record)
