"""Offline native-XLS and exact-order recovery regressions."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import xlrd
import analytics_identifiers
import data_maintenance
import jst_auto_order as service
import jst_auto_order_store as store
import jst_auto_order_template as workbook
import jst_batches
import jst_exports
import jst_orders
import order_lifecycle as lc
import payment_shop_names
import test_jst
import work_repository as wr


CONTACT = {'buyer':'00012345','recipient':'合成收货人','phone':'01234567890','province':'山东省',
           'city':'济南市','district':'历下区','address':'合成测试路1号'}


class NativeXLS(unittest.TestCase):
    def test_preserves_original_header_order_and_writes_text_fields(self):
        rows=[{'sku':key,'quantity':qty,'price':'0.75'} for key,qty in
              [('SKU001',10),('@SKU002',20),('0003',30),('@SKU004',40),('ZERO',0)]]
        lines,stats=workbook.order_lines(rows)
        data,_=workbook.make_file(CONTACT,'BH'+'A'*24,lines,stats)
        original=xlrd.open_workbook(str(workbook.TEMPLATE)).sheet_by_index(0)
        b=xlrd.open_workbook(file_contents=data,formatting_info=True);s=b.sheet_by_name(workbook.SHEET)
        self.assertEqual((s.nrows,s.ncols),(2,39));self.assertEqual(s.row_values(0),original.row_values(0))
        self.assertEqual(s.row_values(1)[20:26],['SKU001*10,@SKU002*20,0003*30,@SKU004*40','','','','','否'])
        self.assertEqual(stats['sku_cell_chars'],workbook.cell_length(s.cell_value(1,20)))
        self.assertEqual(s.cell_value(1,29),'现场取货')
        self.assertEqual((s.cell_value(1,2),s.cell_value(1,12)),(CONTACT['buyer'],CONTACT['phone']))
        for column in (0,2,12,20):
            self.assertEqual(s.cell_type(1,column),xlrd.XL_CELL_TEXT)
            self.assertEqual(b.format_map[b.xf_list[s.cell_xf_index(1,column)].format_key].format_str,'@')

    def test_utf16_size_before_workbook_and_actual_bytes_after_workbook(self):
        too_long=[{'sku':'商'*32766,'quantity':1,'price':1}]
        lines,stats=workbook.order_lines(too_long)
        with self.assertRaisesRegex(ValueError,'U 列商品串 32,768'):
            workbook.make_file(CONTACT,'BH'+'A'*24,lines,stats,template='must-not-read.xls')
        lines,stats=workbook.order_lines([{'sku':'0','quantity':1,'price':1}])
        with patch.object(workbook,'MAX_BYTES',100):
            with self.assertRaisesRegex(ValueError,'超过聚水潭 3 MB'):
                workbook.make_file(CONTACT,'BH'+'A'*24,lines,stats)
        self.assertEqual(workbook.cell_length('😀'),2)

    def test_single_commodity_rows_preserve_all_2000_items_in_one_order(self):
        order='BH'+'B'*24
        items=[{'sku': ('@' if i%2 else '0')+f'{i:019d}', 'quantity':i%3+1, 'price':'2.10'}
               for i in range(2000)]
        lines,stats=workbook.order_lines(items)
        self.assertGreater(stats['sku_cell_chars'],workbook.MAX_CELL)
        data,_=workbook.make_file(CONTACT,order,lines,stats,import_format=workbook.SINGLE)
        sheet=xlrd.open_workbook(file_contents=data).sheet_by_name(workbook.SHEET)
        self.assertEqual((sheet.nrows,sheet.ncols),(2001,39))
        self.assertLess(len(data),workbook.MAX_BYTES)
        for i,(sku,qty,_) in enumerate(lines,1):
            self.assertEqual([sheet.cell_value(i,c) for c in (0,2,12,20,23,24,25,29)],
                             [order,CONTACT['buyer'],CONTACT['phone'],sku,int(qty),'','否','现场取货'])
            self.assertEqual(sheet.cell_type(i,20),xlrd.XL_CELL_TEXT)
        with patch.object(workbook,'MAX_DATA_ROWS',1999):
            with self.assertRaisesRegex(ValueError,'行限制'):
                workbook.make_file(CONTACT,order,lines,stats,import_format=workbook.SINGLE)


class Portal:
    """Controlled exact-match platform. The live integration is gated separately."""
    def __init__(self):
        self.orders={}
        self.calls=[]
        self.disconnected=False
        self.audit_rejected=False
        self.price_diff=False
        self.quantity_diff=False
    def action(self, name, order, **kwargs):
        payload=order['payload']
        wh=payload['head']['warehouse']
        binding={'warehouse':wh,'shop':payload['shop']['name'],'company':wh+'测试企业',
                 'coid':'1' if wh=='河北仓' else '2','uid':'3' if wh=='河北仓' else '4',
                 'shop_id':'22' if wh=='河北仓' else '44','version':7}
        key=order['online']
        result={'online':key,'binding':binding,'lines':[list(r) for r in payload['lines']],
                'amount':payload['stats']['amount'],'issues':False,'contact_ok':True,'permit':'only-once'}
        self.calls.append((name,key))
        if self.price_diff:
            result['lines'][0][2]='8'
            result['amount']='180.5' if name=='preview' else '192'
        if self.quantity_diff and name in ('preview','stage_import'):
            result['lines'][0][1]='1'
        if name in ('lookup','stage_audit'):
            existing=self.orders.get(key)
            if not existing:return {'found':False}
            return {**result,**existing,'found':True}
        if name in ('preview','stage_import') and key in self.orders:
            return {'exists':True}
        return result
    def request(self,name,*,online,**kwargs):
        self.calls.append((name,online))
        if name=='import':
            if online in self.orders:raise AssertionError('duplicate import')
            self.orders[online]={'internal':str(6001+len(self.orders)),'status':'awaiting'}
            if self.disconnected:raise service.PlatformCheck('合成断线')
        elif self.audit_rejected:
            self.orders[online].update(audit_rejected=True)
        else:
            self.orders[online].update(status='audited',native_status='已客审待财审')


class AutoOrders(unittest.TestCase):
    def setUp(self):
        self.f=test_jst.Service();self.f.setUp();self.addCleanup(self.f.tearDown)
        self.path=self.f.path;self.head=self.f.h;self.portal=Portal()
        _,self.gid=self.f.generated()
        self.selected=[{'gid':self.gid,'head':self.head}]

    def configure(self, head, values=CONTACT):
        data={'names':{head['store']:head.get('name','测试店')},'records':{}}
        plan=payment_shop_names.prepare(data,[{'store':head['store'],'warehouse':head['warehouse'],'value':head['warehouse']+'合成店铺'}])
        payment_shop_names.save(self.path,plan)
        service.save_profile(self.path,head['store'],head['warehouse'],values,None)

    def prepared(self):
        self.configure(self.head)
        r=service.prepare(self.path,self.selected)
        self.assertFalse(r['failed'])
        return r['ready']

    def read(self,key,workbook=False):
        with closing(wr.read_connection(self.path)) as db:return store.read(db,key,workbook=workbook)

    def test_missing_profile_mentions_exact_warehouse_and_fields(self):
        self.configure(self.head,{**CONTACT,'phone':'','buyer':''})
        receipt=service.prepare(self.path,self.selected)
        self.assertFalse(receipt['ready'])
        issue=receipt['failed'][0]
        self.assertIn('河北仓',issue['order'])
        self.assertIn('买家帐号、手机',issue['reason'])
        self.assertEqual(store.list_orders(self.path),[])

    def test_invalid_warehouse_phone_is_reported_before_file_or_task_creation(self):
        self.configure(self.head,{**CONTACT,'phone':'0123456789'})
        receipt=service.prepare(self.path,self.selected)
        self.assertFalse(receipt['ready'])
        self.assertIn('河北仓',receipt['failed'][0]['order'])
        self.assertIn('收货手机须为11位数字',receipt['failed'][0]['reason'])
        self.assertEqual(store.list_orders(self.path),[])

    def test_large_generation_routes_to_single_and_rejects_preview_without_grouping_proof(self):
        self.configure(self.head)
        sku='@'+('1234567890'*2)
        lines,stats=workbook.order_lines([{'sku':sku+str(i),'quantity':1,'price':2} for i in range(2000)])
        with patch.object(workbook,'order_lines',return_value=(lines,stats)):
            result=service.prepare(self.path,self.selected)
        self.assertFalse(result['failed'])
        online,=result['ready']
        order=self.read(online,workbook=True)
        self.assertEqual(order['payload']['import_format'],workbook.SINGLE)
        self.assertEqual(xlrd.open_workbook(file_contents=order['workbook']).sheet_by_index(0).nrows,2001)
        for count in (None,2):
            evidence={**self.portal.action('preview',order),'order_count':count,'import_format':workbook.SINGLE}
            with self.assertRaisesRegex(ValueError,'缺少一张整单'):
                service._preview_for_submit(order,evidence)
        checked=service._preview_for_submit(order,{**evidence,'order_count':1})
        self.assertEqual(checked['amount'],'4000')
        self.assertFalse(service._same_order({**checked,'amount':'4001'},checked))

    def test_one_order_phone_override_keeps_saved_store_profile(self):
        self.configure(self.head)
        result=service.prepare(self.path,self.selected,order_phone='10000000000')
        self.assertFalse(result['failed'])
        order=self.read(result['ready'][0],workbook=True)
        sheet=xlrd.open_workbook(file_contents=order['workbook']).sheet_by_name(workbook.SHEET)
        self.assertEqual(sheet.cell_value(1,12),'10000000000')
        self.assertEqual(order['payload']['profile']['values']['phone'],CONTACT['phone'])
        with closing(wr.read_connection(self.path)) as db:
            self.assertEqual(store.profile(db,self.head['store'],self.head['warehouse'])['values'],CONTACT)

    def test_platform_price_and_amount_changes_are_informational(self):
        keys=self.prepared();self.portal.price_diff=True
        self.assertEqual(service.process(self.path,keys,self.portal,'preview')[0]['stage'],'PREVIEWED')
        self.assertEqual(self.read(keys[0])['proof']['amount'],'180.5')
        self.assertEqual(service.process(self.path,keys,self.portal,'submit')[0]['stage'],'AUDITED')

    def test_bulk_address_edit_invalidates_old_preview_and_rebuilds_same_order(self):
        import jst_delivery_profiles as profiles
        import jst_delivery_profiles_xlsx as profiles_xlsx
        from contextlib import closing
        from openpyxl import load_workbook

        keys=self.prepared()
        service.process(self.path,keys,self.portal,'preview')
        snapshot=profiles.directory(self.path,{})
        path=self.f.f.root/'new-address.xlsx'
        profiles_xlsx.export(path,snapshot)
        with closing(load_workbook(path)) as book:
            book[profiles_xlsx.SHEET]['J2']='批量更正后的合成地址'
            book.save(path)
        store.save_profiles(self.path,profiles_xlsx.plan(path,snapshot))
        result=service.process(self.path,keys,self.portal,'submit')[0]
        self.assertIn('收货资料已更新',result['reason'])
        self.assertNotIn('import',[action for action,_ in self.portal.calls])
        self.assertEqual(service.prepare(self.path,self.selected)['ready'],keys)
        order=self.read(keys[0],workbook=True)
        sheet=xlrd.open_workbook(file_contents=order['workbook']).sheet_by_name(workbook.SHEET)
        self.assertEqual(sheet.cell_value(1,17),'批量更正后的合成地址')
        self.assertEqual(order['stage'],'PREPARED')

    def test_quantity_change_and_existing_order_stop_before_intent(self):
        keys=self.prepared();self.portal.quantity_diff=True
        issue=service.process(self.path,keys,self.portal,'preview')[0]
        self.assertIn('逐项数量',issue['reason'])
        self.assertEqual(self.read(keys[0])['stage'],'PREPARED')
        self.portal.quantity_diff=False
        self.portal.orders[keys[0]]={'internal':'98765','status':'awaiting'}
        issue=service.process(self.path,keys,self.portal,'preview')[0]
        self.assertIn('已存在',issue['reason'].replace('已有','已存在'))
        self.assertFalse(any(op in ('import','audit') for op,_ in self.portal.calls))

    def test_submit_duplicate_preserves_order_and_business_payment(self):
        keys=self.prepared()
        self.assertEqual(service.prepare(self.path,self.selected)['ready'],keys)
        before=jst_orders.get(self.path,self.gid)
        first=jst_exports.export_checked(self.path,[self.gid],self.f.f.root/'manual')['files'][0]
        service.process(self.path,keys,self.portal,'preview')
        result=service.process(self.path,keys,self.portal,'submit')[0]
        self.assertEqual(result['stage'],'AUDITED')
        service.process(self.path,keys,self.portal,'submit');service.process(self.path,keys,self.portal,'recover')
        self.assertEqual([op for op,_ in self.portal.calls].count('import'),1)
        self.assertEqual([op for op,_ in self.portal.calls].count('audit'),1)
        with closing(wr.read_connection(self.path)) as db:
            self.assertIsNone(lc.current(db,self.head,'payment'))
            self.assertEqual(jst_orders.locked(db,self.head),self.gid)
            self.assertEqual(analytics_identifiers.registered(db,self.head)['number'],'6001')
        batch=jst_orders.get(self.path,self.gid)
        self.assertEqual(batch,before)
        r=jst_exports.export_checked(self.path,[self.gid],self.f.f.root/'manual-again')
        import openpyxl
        for path in (first['path'],r['files'][0]['path']):
            source=openpyxl.load_workbook(path,read_only=True)
            try:self.assertEqual(list(source.active.values),[('商品编码','单价','数量','订单号'),('0001',2,40,self.head['number'])])
            finally:source.close()

    def test_submit_disconnect_restores_exact_order_and_audits_without_reimport(self):
        with patch.object(service,'_online',return_value='BH'+'A'*24):
            keys=self.prepared()
        service.process(self.path,keys,self.portal,'preview')
        self.portal.disconnected=True
        result=service.process(self.path,keys,self.portal,'submit')[0]
        self.assertEqual(result['stage'],'IMPORT_UNKNOWN')
        unresolved=self.read(keys[0],workbook=True)
        self.assertEqual(service.prepare(self.path,self.selected),{'ready':keys,'failed':[]})
        self.assertEqual(self.read(keys[0],workbook=True),unresolved)
        with self.assertRaisesRegex(ValueError,'待核'):
            jst_orders.cancel_generated(self.path,[self.gid],'合成撤销')
        plan=data_maintenance.prepare(self.path,[self.head])
        with self.assertRaisesRegex(ValueError,'结果待核'):data_maintenance.execute(self.path,plan,True)
        restarted=Portal();restarted.orders=self.portal.orders
        result=service.process(self.path,keys,restarted,'recover')[0]
        self.assertEqual(result['stage'],'IMPORTED')
        result=service.process(self.path,keys,restarted,'audit')[0]
        self.assertEqual(result['stage'],'AUDITED')
        self.assertNotIn('import',[action for action,_ in restarted.calls])

    def test_audit_rejected_can_continue_on_original_internal_order(self):
        keys=self.prepared()
        service.process(self.path,keys,self.portal,'preview')
        self.portal.audit_rejected=True
        result=service.process(self.path,keys,self.portal,'submit')[0]
        self.assertEqual(result['stage'],'IMPORTED')
        self.assertIn('普通审核未通过',result['reason'])
        self.assertEqual(self.read(keys[0])['internal'],'6001')
        self.portal.audit_rejected=False
        self.assertEqual(service.process(self.path,keys,self.portal,'audit')[0]['stage'],'AUDITED')
        self.assertEqual(self.read(keys[0])['internal'],'6001')
        self.assertEqual([op for op,_ in self.portal.calls].count('import'),1)

    def test_recovers_legacy_audit_stopped_before_dispatch(self):
        keys=self.prepared()
        service.process(self.path,keys,self.portal,'preview')
        request=self.portal.request

        def stop_before_click(name, **kwargs):
            if name=='audit':
                raise service.PlatformCheck('原订单商品详情尚未关闭')
            return request(name,**kwargs)

        with patch.object(self.portal,'request',stop_before_click):
            failed=service.process(self.path,keys,self.portal,'submit')[0]
        self.assertEqual(failed['stage'],'AUDIT_UNKNOWN')
        with patch.object(self.portal,'action',side_effect=service.PlatformCheck('合成回查暂不可用')):
            self.assertEqual(service.process(self.path,keys,self.portal,'recover')[0]['stage'],'AUDIT_UNKNOWN')
        self.assertEqual(self.read(keys[0])['reason'],'原订单商品详情尚未关闭')
        self.assertEqual(service.process(self.path,keys,self.portal,'recover')[0]['stage'],'IMPORTED')
        self.assertEqual(service.process(self.path,keys,self.portal,'audit')[0]['stage'],'AUDITED')
        self.assertEqual(self.read(keys[0])['internal'],'6001')
        self.assertEqual([op for op,_ in self.portal.calls if op in ('import','audit')],['import','audit'])

    def test_recovery_accepts_customer_audited_awaiting_finance(self):
        keys=self.prepared()
        service.process(self.path,keys,self.portal,'preview')
        self.portal.disconnected=True
        service.process(self.path,keys,self.portal,'submit')
        self.portal.orders[keys[0]].update(status='review',native_status='已客审待财审')

        result=service.process(self.path,keys,self.portal,'recover')[0]
        self.assertEqual(result['stage'],'AUDITED')
        record=self.read(keys[0])
        self.assertEqual((record['stage'],record['platform_status'],record['internal']),('AUDITED','audited','6001'))
        self.assertEqual(record['reason'],'普通审核已完成，聚水潭状态为已客审待财审')
        with closing(wr.read_connection(self.path)) as db:
            detail=db.execute("SELECT detail FROM jst_events WHERE action='聚水潭原单核对' ORDER BY id DESC LIMIT 1").fetchone()
            self.assertEqual(wr.loads(detail[0])['native_status'],'已客审待财审')
            self.assertIsNone(lc.current(db,self.head,'payment'))
        self.assertEqual([op for op,_ in self.portal.calls if op in ('import','audit')],['import'])

    def test_recovery_does_not_accept_shipping_or_unverified_status_as_ordinary_audit(self):
        keys=self.prepared()
        service.process(self.path,keys,self.portal,'preview')
        self.portal.disconnected=True
        service.process(self.path,keys,self.portal,'submit')
        for native_status in ('发货中(打单拣货)','已发货',None):
            with self.subTest(native_status=native_status):
                self.portal.orders[keys[0]].update(status='audited',native_status=native_status)
                result=service.process(self.path,keys,self.portal,'recover')[0]
                self.assertEqual(result['stage'],'REVIEW')
                record=self.read(keys[0])
                self.assertEqual((record['platform_status'],record['internal']),('review','6001'))
                hint=f'后续发货环节（{native_status}）' if native_status else '未核实聚水潭普通审核的完成状态'
                self.assertIn(hint,record['reason'])
        self.assertEqual([op for op,_ in self.portal.calls if op in ('import','audit')],['import'])

    def test_changed_return_at_final_boundary_rejects_old_order(self):
        keys=self.prepared()
        service.process(self.path,keys,self.portal,'preview')
        action=self.portal.action
        def revised(name,order,**kw):
            if name=='stage_import':self.f.f.save('return',[{'门店确认数量':20}])
            return action(name,order,**kw)
        with patch.object(self.portal,'action',revised):
            result=service.process(self.path,keys,self.portal,'submit')[0]
        self.assertIn('回传',result['reason'])
        self.assertEqual(result['stage'],'PREVIEWED')
        self.assertEqual(self.portal.orders,{})

    def test_review_confirmation_cannot_submit_a_later_preview(self):
        keys=self.prepared()
        service.process(self.path,keys,self.portal,'preview')
        confirmed=store.list_orders(self.path)[0]
        service.process(self.path,keys,self.portal,'preview')
        result=service.process(self.path,keys,self.portal,'submit',
                               approved={confirmed['online']:confirmed['sequence']})[0]
        self.assertIn('任务已更新',result['reason'])
        self.assertFalse(self.portal.orders)
        self.assertNotIn('stage_import',[op for op,_ in self.portal.calls])

    def dual_selection(self):
        south=next(h for h in self.f.f.heads if h['warehouse']=='山东仓')
        self.f.f.save('return',[{'补货单号':south['number'],'供货仓':'山东仓','商品编码':'0002','门店确认数量':18}])
        catalog=test_jst.catalog('山东仓')
        catalog['items']['0002']={**catalog['items'].pop('0001'),'商品编码':'0002'}
        from warehouse_catalog import revision
        catalog['revision']=revision(catalog['items'])
        r=jst_batches.execute(self.path,jst_batches.preview_new(self.path,[south],{'山东仓':catalog}))
        self.assertEqual(len(r['generated']),1)
        self.configure(self.head)
        self.configure(south,{**CONTACT,'buyer':'000888','address':'合成山东收货点'})
        return [*self.selected,{'head':south,'gid':r['generated'][0]}]

    def test_pending_audit_blocks_only_selected_scope_and_reports_uncommitted_deletion(self):
        from ui_work_io import Session
        from work_failures import WorkFailure

        selected=self.dual_selection()
        heads=[r['head'] for r in selected]
        keys=service.prepare(self.path,selected)['ready']
        service.process(self.path,keys[:1],self.portal,'preview')
        self.portal.disconnected=True
        service.process(self.path,keys[:1],self.portal,'submit')
        self.assertEqual(service.process(self.path,keys[:1],self.portal,'recover')[0]['stage'],'IMPORTED')
        progress=[]
        with Session(self.path) as session:
            old_plan=session.call('delete_preview',heads,progress.append)
            # Audit starts between the user's preview and confirmation.
            with closing(store.connection(self.path)) as db,db:
                original=store.read(db,keys[0])
                store.update(db,original,'AUDIT_UNKNOWN',reason='合成普通审核结果待核')
            before=[self.read(key,workbook=True) for key in keys]
            with self.assertRaises(WorkFailure) as failed:
                session.call('delete_orders',{'plan':old_plan,'ack':True},progress.append)
            self.assertEqual(failed.exception.evidence['commit_state'],'not_started')
            self.assertIn('未提交删除',str(failed.exception))
            with self.assertRaises(WorkFailure) as blocked:
                session.call('delete_preview',heads,progress.append)
            for error in (failed.exception,blocked.exception):
                self.assertIn(heads[0]['number'],str(error))
                self.assertIn('审核结果待核',str(error))
                self.assertIn('6001',str(error))
                self.assertNotIn(heads[1]['number'],str(error))
                self.assertNotIn('后续导出',str(error))
            self.assertEqual([self.read(key,workbook=True) for key in keys],before)
            south=session.call('delete_preview',heads[1:],progress.append)
            removed=session.call('delete_orders',{'plan':south,'ack':True},progress.append)
        self.assertEqual(removed['orders'],[heads[1]['number']])
        self.assertTrue(any('删除已提交' in message for message in progress))
        self.assertEqual(self.read(keys[0],workbook=True),before[0])
        self.assertIsNone(self.read(keys[1]))

    def test_delete_preview_recovers_only_selected_pending_task(self):
        from runtime_support import Cancelled
        from ui_work_io import Session
        from work_failures import WorkFailure

        selected=self.dual_selection()
        north,south=service.prepare(self.path,selected)['ready']
        service.process(self.path,[north,south],self.portal,'preview')
        self.portal.disconnected=True
        service.process(self.path,[north,south],self.portal,'submit')
        head=selected[0]['head']
        original,untouched=[self.read(key,workbook=True) for key in (north,south)]
        self.assertEqual(original['stage'],'IMPORT_UNKNOWN')
        self.assertEqual(untouched['stage'],'IMPORT_UNKNOWN')
        self.portal.calls.clear()
        lease=SimpleNamespace(start=lambda:None,close=lambda **_:True)
        owner=SimpleNamespace(acquire=lambda:lease)

        with Session(self.path) as session, patch.object(service,'Browser',return_value=self.portal):
            with self.assertRaisesRegex(WorkFailure,'签发已变化'):
                session.call('delete_preview',{'heads':[{**head,'revision':head['revision']+1}],
                                               'recover_pending':True},lambda _:None)
            waiting=session.call('delete_preview',{'heads':[head],'recover_pending':True},lambda _:None)
            self.assertEqual(waiting,{'pending_jst_orders':{north:original['sequence']}})
            self.assertEqual(self.read(north,workbook=True),original)

            cancel=Event()
            lookup=self.portal.action
            def interrupted(*args,**kwargs):
                result=lookup(*args,**kwargs)
                cancel.set()
                return result
            with patch.object(self.portal,'action',side_effect=interrupted), self.assertRaises(Cancelled):
                service.recover_for_deletion(self.path,waiting['pending_jst_orders'],owner,cancel=cancel)
            self.assertEqual(self.read(north,workbook=True),original)

            retrying=Event()
            attempts=[]
            commit=service.commit
            def traced_commit(db,cancel):
                def trace(statement):
                    if statement=='COMMIT':
                        attempts.append(statement)
                        if len(attempts)==2:retrying.set()
                db.set_trace_callback(trace)
                return commit(db,cancel)

            with patch.object(service,'commit',side_effect=traced_commit), ThreadPoolExecutor(1) as pool, \
                    closing(wr.read_connection(self.path)) as reader:
                reader.execute('BEGIN')
                self.assertIsNotNone(reader.execute('SELECT 1 FROM jst_auto_orders WHERE online=?',(north,)).fetchone())
                future=pool.submit(service.recover_for_deletion,self.path,waiting['pending_jst_orders'],owner)
                try:
                    self.assertTrue(retrying.wait(5),'本地保存没有等待读锁释放')
                finally:
                    reader.rollback()
                future.result(timeout=5)
            plan=session.call('delete_preview',[head],lambda _:None)
            self.assertEqual([h['number'] for h in plan['scopes']],[head['number']])
            self.assertEqual(plan['parents'][0]['remaining_warehouses'],[selected[1]['head']['warehouse']])
            with self.assertRaisesRegex(WorkFailure,'请明确核对外部采购/已下单影响'):
                session.call('delete_orders',{'plan':plan,'ack':False},lambda _:None)

        self.assertEqual(self.portal.calls,[('lookup',north),('lookup',north)])
        self.assertEqual(self.read(north)['stage'],'IMPORTED')
        self.assertEqual(self.read(north)['sequence'],original['sequence']+1)
        self.assertEqual(self.read(south,workbook=True),untouched)

    def test_one_store_two_warehouses_have_independent_profiles_and_submit_keys(self):
        selected=self.dual_selection()
        result=service.prepare(self.path,selected)
        self.assertFalse(result['failed']);keys=result['ready']
        self.assertEqual(len(set(keys)),2)
        service.process(self.path,keys,self.portal,'preview')
        self.assertEqual([r['stage'] for r in service.process(self.path,keys,self.portal,'submit')],['AUDITED','AUDITED'])
        for key,warehouse,buyer in zip(keys,('河北仓','山东仓'),('00012345','000888')):
            record=self.read(key,workbook=True)
            self.assertEqual(record['proof']['binding']['warehouse'],warehouse)
            sheet=xlrd.open_workbook(file_contents=record['workbook']).sheet_by_index(0)
            self.assertEqual(sheet.cell_value(1,2),buyer)

    def test_reprepares_both_warehouses_with_existing_online_numbers(self):
        selected=self.dual_selection()
        keys=['BH'+'A'*24,'BH'+'B'*24]
        with patch.object(service,'_online',side_effect=keys):
            self.assertEqual(service.prepare(self.path,selected),{'ready':keys,'failed':[]})
        service.process(self.path,keys[:1],self.portal,'preview')
        originals=[self.read(key,workbook=True) for key in keys]
        self.assertEqual([row['stage'] for row in originals],['PREVIEWED','PREPARED'])
        self.assertEqual(service.prepare(self.path,selected),{'ready':keys,'failed':[]})
        self.assertEqual([self.read(key,workbook=True) for key in keys],originals)
        self.assertEqual(len(store.list_orders(self.path)),2)

    def test_new_warehouse_cannot_take_another_warehouses_online_number(self):
        north,south=self.dual_selection()
        key='BH'+'A'*24
        with patch.object(service,'_online',return_value=key):
            self.assertEqual(service.prepare(self.path,[north]),{'ready':[key],'failed':[]})
            original=self.read(key,workbook=True)
            result=service.prepare(self.path,[south])
        self.assertFalse(result['ready'])
        self.assertIn('线上单号已关联其他仓单',result['failed'][0]['reason'])
        self.assertEqual(self.read(key,workbook=True),original)
