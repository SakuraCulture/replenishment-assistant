"""Opt-in actual EXE acceptance; synthetic fixture, closed transport, no browser."""
import json,os,sys,threading,time,traceback
from contextlib import closing
from pathlib import Path
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication,QCheckBox,QPushButton,QMessageBox,QDialog
from PySide6.QtTest import QTest


def pending_jst_check(win,head,messages,confirmations,choice):
    import jst_auto_order_store as auto_orders
    import jst_order_store as generations
    import lifecycle_store
    import normal_orders as normal
    import purchases
    import work_repository as work
    from catalog_session_ui import manager as catalog_manager
    from catalog_sync_client import ComponentError
    from jst_order_confirmation import AUDITED,AWAITING
    from lifecycle_rules import JST_ORDER
    from ui_work_io import Session
    from work_failures import WorkFailure

    path=win.work_repository.path
    online='BH'+'D'*24
    internal='90001'
    gid='synthetic-generation'
    binding={'warehouse':head['warehouse'],'shop':'合成聚水潭店铺','company':'合成企业',
             'coid':'1','uid':'2','shop_id':'3'}
    lines=[['0001','3','5']]
    generation={'head':head,'gid':gid,'basis':{'dependencies':{}},'rows':[{'sku':'0001','name':'合成商品'}]}
    generation_digest=generations.digest(generation)
    payload={'head':head,'online':online,'gid':gid,'shop':{'name':binding['shop']},'lines':lines,
             'profile':{'values':{'buyer':'synthetic'}},'generation':{'digest':generation_digest}}
    with closing(win.work_repository.connect()) as db,db:
        generations.schema(db)
        db.execute('INSERT INTO jst_generations VALUES(?,?,?,?,?,?,?,?,?)',
                   (gid,gid,head['oid'],head['revision'],head['warehouse'],head['day'],work.now(),
                    work.dumps(generation),generation_digest))
        db.execute('INSERT INTO jst_active VALUES(?,?,?)',(head['oid'],head['warehouse'],gid))
        auto_orders.schema(db)
        db.execute("INSERT INTO jst_auto_orders VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (online,head['oid'],head['warehouse'],payload['gid'],'IMPORTED',0,work.now(),work.now(),
                    work.dumps(payload),auto_orders.token(payload),'fixture',b'fixture',work.dumps({'binding':binding}),
                    internal,'awaiting','合成已导入'))
    with Session(path) as session:
        plan=session.call('delete_preview',[head],lambda _:None)
        with closing(win.work_repository.connect()) as db,db:
            task=auto_orders.read(db,online)
            auto_orders.update(db,task,'AUDIT_UNKNOWN',reason='合成审核结果待核')
        try:
            session.call('delete_orders',{'plan':plan,'ack':True},lambda _:None)
        except WorkFailure as error:
            assert error.evidence['commit_state']=='not_started' and '未提交删除' in str(error)
        else:
            raise AssertionError('待核仓单在确认后仍被删除')

    calls=[]
    status='login'

    class LookupClient:
        def __init__(self,folder):
            self.running=False
            self.error=None

        def start(self):
            self.running=True

        def close(self,timeout=.5):
            self.running=False
            return True

        def request(self,action,**values):
            if action=='session.release':return {'idle':True}
            assert action=='order.lookup',action
            spec=values['spec']
            assert (spec['warehouse'],spec['online'],spec['internal'],spec['binding'])==(head['warehouse'],online,internal,binding)
            calls.append(action)
            if status=='login':raise ComponentError('LOGIN_REQUIRED','合成未登录')
            if status=='missing':return {'found':False}
            return {'found':True,'online':online,'binding':binding,'internal':internal,'native_status':status,
                    'status':'audited' if status==AUDITED else 'awaiting','contact_ok':True,'lines':lines,'amount':'15'}

    catalog_manager(win).session.factory=LookupClient
    win.browser_status.timer.stop()
    win.show_section(3)
    page=win.followup
    page.checked={(head['oid'],head['warehouse']):head}
    maintenance=next(b for b in page.findChildren(QPushButton) if b.text()=='订单维护')
    delete_button=next(b for b in page.findChildren(QPushButton) if b.text()=='彻底删除勾选订单')
    maintenance.click()
    assert delete_button.isVisible() and delete_button.isEnabled()
    failures=[]
    for status,why in (('login','请登录该仓专用浏览器'),('missing','尚未查到唯一订单'),(AWAITING,AWAITING)):
        delete_button.click()
        title,message=messages.pop()
        assert title=='未完成删除' and all(t in message for t in (head['number'],internal,'审核结果待核','未提交删除',why))
        assert '归档已提交' not in message and not confirmations
        failures.append(message)
    status=AUDITED
    delete_button.click()
    assert len(confirmations)==1 and not messages
    assert confirmations[0]=={'requires_scope_confirmation':True,'delete_enabled_before_ack':False}
    assert calls==['order.lookup']*4
    with closing(win.work_repository.connect()) as db:
        record=auto_orders.read(db,online)
        assert (record['stage'],record['internal'])==('AUDITED',internal)
        saved=lifecycle_store.decode(lifecycle_store.version_row(db,head,JST_ORDER))
        assert saved['payload']['native_status']==AUDITED and saved['payload']['reference']['internal']==internal
        assert lifecycle_store.version_row(db,head,'payment') is None
    assert normal.Repository(win.work_repository).get(head['day'],head['store']) is not None
    before={h['number'] for h in purchases.targets(path)}
    choice[0]='delete'
    delete_button.click()
    assert len(confirmations)==2 and calls==['order.lookup']*4
    assert messages.pop()[0]=='已彻底删除' and not messages
    assert {h['number'] for h in purchases.targets(path)}==before-{head['number']}
    with closing(win.work_repository.connect()) as db:
        assert auto_orders.read(db,online) is None
    return {'checks':['确认后出现待核时回滚整个删除并准确报告未提交',
                      '删除按钮自动沿原单号查询，并分别显示未登录、查无唯一原单和待审核',
                      '回查客审完成后打开原删除确认，取消仍保留订单',
                      '再次确认只删除选中的本地仓单，无待核项时不连接平台查询',
                      '平台只读回查，保留客审依据且未确认付款'],
            'blocked_dialogs':failures,'lookup_actions':calls,'confirmation':confirmations[0],'passed':True}


def start_smoke(app,win,args):
    out=Path(args[0]).resolve();fixture=json.loads((out/'fixture.json').read_text('utf-8'))
    if not fixture.get('synthetic') or Path(os.environ.get('REPLENISHMENT_DATA_DIR','')).resolve()!=out/'settings':raise ValueError('Requires isolated fixture')
    app.setQuitOnLastWindowClosed(False)
    def execute():
        report={'passed':False,'frozen_exe':bool(getattr(sys,'frozen',False)),'live_platform':False,'checks':[]};checks=report['checks']
        release=threading.Event();entered=threading.Event();errors=[];messages=[];thread=None;client=None
        controls=QTimer(win);controls.setInterval(20);choice=['cancel'];confirmations=[]
        def wait(predicate):
            end=time.monotonic()+30
            while not predicate() and time.monotonic()<end:app.processEvents();QTest.qWait(10)
            if not predicate():raise TimeoutError('UI/worker wait')
        def confirm():
            for d in QApplication.topLevelWidgets():
                if not d.isVisible():continue
                if isinstance(d,QMessageBox):messages.append((d.windowTitle(),d.text()));d.accept()
                elif isinstance(d,QDialog) and d.windowTitle()=='彻底删除订单及关联数据':
                    boxes=d.findChildren(QCheckBox);assert len(boxes)==1
                    go=next(b for b in d.findChildren(QPushButton) if b.text()=='永久删除上述订单')
                    confirmations.append({'requires_scope_confirmation':not boxes[0].isChecked(),
                                          'delete_enabled_before_ack':go.isEnabled()})
                    if choice[0]=='cancel':d.reject();continue
                    boxes[0].setChecked(True);go.click()
        try:
            import data_maintenance_ui as ui,normal_orders as no,purchases,deletion_barrier
            from aoxiang_workflow_runner import Runner
            from aoxiang_client import ProcessClient,ComponentError
            from aoxiang_ui_common import component_root
            from aoxiang_workflow_ui import QueueDialog
            win.auto_check.stop();win.jst_generator.stopped=True;win.jst_generator.timer.stop()
            manager=win.aoxiang_workflows;manager.stopping=True;manager.timer.stop()
            heads=purchases.targets(win.work_repository.path);gone=next(h for h in heads if h['oid']=='oid');kept=next(h for h in heads if h['oid']=='other')
            if 'jst-pending' in args[1:]:
                win.jst_fulfillment_runner.timer.stop()
                controls.timeout.connect(confirm);controls.start()
                report.update(pending_jst_check(win,gone,messages,confirmations,choice))
                return
            before=no.Repository(win.work_repository).get(kept['day'],kept['store'])
            package_id='purchase-'+'a'*32
            deletion_barrier.register_package(win.work_repository.path,'oid',package_id)
            private=out/'settings/aoxiang/purchase-packages'/package_id/'付款采购依据.json';private.parent.mkdir(parents=True)
            private.write_text('{"synthetic_private_payment":true}','utf-8')
            legacy_id='purchase-'+'b'*32;legacy=private.parent.parent/legacy_id/'采购任务.json';legacy.parent.mkdir()
            legacy.write_text(json.dumps({'id':legacy_id,'basis':{'oid':'oid'},'rows':[{'name':'synthetic product'}]}),'utf-8')
            import export_receipts
            export_receipt=export_receipts.begin(win.work_repository.path,'archive',[{'path':str(out/'external-original.xlsx'),'state':'已完成','heads':[gone,kept]}])
            job=manager.store.prepare([gone,kept]);calls=[]
            class ClosedClient:
                def request(self,action,**kw):
                    assert action=='configuration';return {}
            runner=Runner(manager.store,ClosedClient(),out,win.work_repository.path,cancel=threading.Event())
            def prepare(job,item,*_):
                oid=item['head']['oid'];calls.append(oid)
                if oid=='oid':entered.set();assert release.wait(45)
                return 'PREPARED',{'proof':oid}
            runner.prepare_item=prepare
            def run():
                try:runner.run(job['id'])
                except Exception:errors.append(traceback.format_exc())
            # Suppliers are a local mapping; no platform transport is provided.
            from aoxiang_directory import Directory
            Directory(manager.store.folder).save_suppliers({'山东仓':'ZXC2025','河北仓':'2078246'})
            thread=threading.Thread(target=run);thread.start();wait(entered.is_set)
            win.followup.checked={('oid',gone['warehouse']):gone,('other',kept['warehouse']):kept}
            controls.timeout.connect(confirm);controls.start()
            ui.remove(win,win,[gone]);assert no.Repository(win.work_repository).get(gone['day'],gone['store']) is not None
            checks.append('取消确认不删除任何订单或任务')
            choice[0]='delete';ui.remove(win,win,[gone])
            assert no.Repository(win.work_repository).get(gone['day'],gone['store']) is None
            assert no.Repository(win.work_repository).get(kept['day'],kept['store'])==before
            assert set(h['oid'] for h in win.followup.checked.values())=={'other'}
            checks+=['运行中订单单次确认即可删除，无平台状态门禁','同店两仓完整删除，未选订单快照和勾选保留']
            release.set();wait(lambda:not thread.is_alive());assert not errors,errors
            current=manager.store.get(job['id']);assert calls==['oid','other'] and len(current['items'])==1
            assert current['items'][0]['result']=={'proof':'other'}
            checks.append('晚到结果不复活或错写，剩余订单继续完成')
            d=QueueDialog(win,manager,job['id']);d.timer.stop();d.show();app.processEvents();d.grab().save(str(out/'剩余批次.png'));d.close();d.deleteLater()
            assert len(deletion_barrier.pending(win.work_repository.path))==1
            client=ProcessClient(component_root(),out/'settings/aoxiang/component-state');client.start()
            assert client.request('purchase.list')['total']==0
            assert deletion_barrier.pending(win.work_repository.path)==[]
            assert not private.exists() and not legacy.exists()
            export_receipts.save(win.work_repository.path,export_receipt)
            saved_receipt=json.loads((export_receipts.directory(win.work_repository.path)/(export_receipt['id']+'.json')).read_text('utf-8'))
            assert saved_receipt['files'][0]['heads']==[kept]
            checks.append('共享导出回执保留未选身份，晚到回执不能恢复已删单')
            checks.append('未提交组件的预览包及旧格式原生身份预览包完整清理')
            private.write_text('{"late_local_producer":true}','utf-8');deletion_barrier.package_finished(win.work_repository.path,'oid',package_id)
            client.request('purchase.list');assert not private.exists() and deletion_barrier.pending(win.work_repository.path)==[]
            checks.append('清理确认后晚到文件重新入队，旧确认不能丢失清理请求')
            try:client.request('orders.retire',operation_id='f'*32,oids=['other']);raise AssertionError('live order retirement accepted')
            except ComponentError as e:assert e.code=='RETIREMENT_NOT_CONFIRMED'
            assert client.close(timeout=20);client=None
            checks+=['实际随包组件纯本地处理离线清理待办并确认','无已提交删除依据时不能退役未选订单']
            assert not any(title in ('未完成删除','订单已删除，页面未完成刷新') for title,_ in messages),messages
            report['messages']=messages;report['passed']=True
        except Exception:report['error']=traceback.format_exc()
        finally:
            release.set()
            if thread:thread.join(10)
            controls.stop()
            if client:client.close(timeout=20)
            win.job_dirty=False;win._discard_close=True;win.close()
            try:wait(lambda:win.aoxiang_workflows.owner is None and win.aoxiang_workflows.session.client is None);report['shutdown_complete']=True
            except Exception:report['passed']=False;report['shutdown_error']=traceback.format_exc()
            build=Path(getattr(sys,'_MEIPASS',Path(__file__).parent))/'build_info.json'
            if build.exists():report['source_commit']=json.loads(build.read_text('utf-8'))['source_commit']
            (out/'acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
            QTimer.singleShot(100,lambda:app.exit(0 if report['passed'] else 1))
    QTimer.singleShot(100,execute)
