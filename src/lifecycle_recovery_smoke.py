"""Explicit offline acceptance: real Qt buttons, workers, files and application exit."""
import copy,json,os,sqlite3,sys,time,traceback
from contextlib import closing
from pathlib import Path
from PySide6.QtCore import QTimer,Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QMessageBox


def start_smoke(app,win,args):
    out=Path(args[0]).resolve();mode=args[1]
    assert json.loads((out/'synthetic.json').read_text('utf-8'))=={'synthetic':True,'live_platform':False}
    path=win.work_repository.path
    assert Path(os.environ['REPLENISHMENT_DATA_DIR']).resolve()==out/'settings' and path.resolve()==out/'settings/workbench.sqlite3'
    win.auto_check.stop();win.jst_generator.stopped=True;win.jst_generator.timer.stop()
    manager=win.aoxiang_workflows;manager.timer.stop();manager.stopping=False
    # Queue buttons operate against the real owner/store. Only dispatch is
    # disabled in this offline acceptance; pipeline original-task behavior has
    # its own real Runner tests with a strictly closed fake transport.
    old_tick=manager.tick;manager.tick=lambda:None
    checks=[];dialogs=[];messages=[];unexpected=[];cancel_recovery=[False]
    fixture=json.loads((out/'fixture.json').read_text('utf-8'));head=fixture['head'];other=fixture['other']
    sd=next(h for h in fixture['heads'] if h['warehouse']=='山东仓')
    import order_lifecycle as lc,lifecycle_recovery as recovery
    import jst_batches as batches,jst_orders as orders,work_repository as wr
    from lifecycle_recovery_ui import RecoveryDialog
    from aoxiang_workflow_ui import QueueDialog
    def require(ok,text):
        if not ok:raise AssertionError(text)
        checks.append(text)
    def write(name,value):(out/name).write_text(json.dumps(value,ensure_ascii=False,indent=2),'utf-8')
    def shot(widget,name):require(widget.grab().save(str(out/(mode+'-'+name+'.png'))),'保存实际窗口证据 '+name)
    def wait(predicate):
        end=time.monotonic()+50
        while not predicate() and time.monotonic()<end:QTest.qWait(20)
        if not predicate():raise AssertionError('Timed out waiting for actual Qt worker')
    def modal():
        d=app.activeModalWidget()
        if cancel_recovery[0] and d and d.windowTitle()=='核实原任务并恢复保存' and hasattr(d,'cancel_btn'):
            cancel_recovery[0]=False;QTest.mouseClick(d.cancel_btn,Qt.MouseButton.LeftButton);return
        if not isinstance(d,QMessageBox):return
        messages.append({'title':d.windowTitle(),'text':d.text(),'details':d.detailedText()})
        allowed=('确认批量上传采购','取消剩余任务','确认生成聚水潭订单')
        if d.windowTitle() in allowed:
            b=d.button(QMessageBox.StandardButton.Yes)
            if b:QTest.mouseClick(b,Qt.MouseButton.LeftButton)
        else:
            unexpected.append(d.windowTitle()+': '+d.text());d.reject()
    timer=QTimer(win);timer.setInterval(20);timer.timeout.connect(modal);timer.start()
    def catalog():
        import warehouse_catalog as wc
        items={'0001':{'商品编码':'0001','商品名称':'合成测试','商品标签':'','分类':'测试','基本售价':2,'出库箱规':1,'商品状态':'启用','可用数':100,'_row':2}}
        return {'schema':1,'warehouse':'河北仓','items':items,'revision':wc.revision(items),'imported_at':wr.now(),'source_path':'synthetic.xlsx','source_sha256':'synthetic','sheet':'商品资料'}
    def prepare_queue(count=1):
        heads=[head,other][:count];p=manager.store.prepare(heads)
        for n in range(count):manager.store.update(p['id'],position=n,changes={'state':'PREPARED','package':{'id':'offline-p-'+str(n)},'summary':{'amount':'1','sku_count':1}})
        return manager.store.update(p['id'],state='READY')
    def plan_for(h,stage,quantity,name):
        import openpyxl,lifecycle_io as io
        file=out/name;b=openpyxl.Workbook();s=b.active;s.append(io.fields(stage))
        row={'补货单号':h['number'],'签发修订':h['revision'],'门店编码':h['store'],'供货仓':h['warehouse'],'商品编码':'0002','商品名称':'测试商品',
             '数量单位':'库存单位',io.QUANTITIES[stage]:quantity,'实际金额':12,'币种':'CNY',
             io.DOCNUM[stage]:'SYN-'+stage,io.DOCDATE[stage]:wr.today()}
        s.append([row.get(k) for k in io.fields(stage)]);b.save(file);b.close()
        info=io.inspect([file])[0];sheet=info['sheets'][0]
        scan=io.scan(stage,[{**info,**sheet,'mapping':{h:i for i,h in enumerate(sheet['headers'])}}])
        return lc.prepare(path,scan,{lc.binding_key(r):h for r in scan['rows']})
    def interrupt(plan):
        original=lc._commit
        def fail(*a,**k):raise OSError('synthetic before business save')
        lc._commit=fail
        try:
            from lifecycle_ui import commit
            try:commit(win,win,plan)
            except Exception as exc:require('synthetic before business save' in str(exc),'真实保存线程保留首次异常')
            else:raise AssertionError('Fault not reached')
        finally:lc._commit=original
        return recovery.recent(path)[0]['id']
    def exercise():
        store=manager.store
        p=prepare_queue();q=QueueDialog(win,manager,p['id']);dialogs.append(q);q.show();QTest.qWait(40)
        q.table.item(0,0).setCheckState(Qt.CheckState.Checked);QTest.mouseClick(q.upload_btn,Qt.MouseButton.LeftButton)
        upload=store.get(q.id);uid=upload['id'];QTest.mouseClick(q.cancel_btn,Qt.MouseButton.LeftButton)
        require(store.get(uid)['state']=='CANCELLED','真实取消按钮只结束未开始上传')
        q.id=p['id'];q.current=None;q.refresh();q.table.item(0,0).setCheckState(Qt.CheckState.Checked)
        QTest.mouseClick(q.upload_btn,Qt.MouseButton.LeftButton)
        require(q.id==uid and store.get(uid)['state']=='QUEUED','实际重新确认按钮衔接同一原上传任务')
        store.update(uid,position=0,changes={'state':'BOUND'});store.update(uid,state='COMPLETED');q.close()
        p=prepare_queue(2);j=store.upload(p['id'],[0,1],p['sequence']);uid=j['id']
        store.update(uid,position=0,changes={'state':'ACCEPTED','purchase_observation':{'uploadAttempted':True,'taskId':'offline-original-task'}})
        q=QueueDialog(win,manager,uid);dialogs.append(q);q.show();QTest.qWait(30)
        QTest.mouseClick(q.cancel_btn,Qt.MouseButton.LeftButton)
        require(store.get(uid)['state']=='PAUSED' and store.get(uid)['items'][1]['state']=='SKIPPED','真实混合批次取消保留已受理任务入口')
        q.refresh();shot(q,'accepted-after-cancel')
        row=q.table.cellWidget(0,8);require(row is not None,'已受理行可继续核对原结果')
        QTest.mouseClick(row,Qt.MouseButton.LeftButton)
        j=store.get(uid);require(j['dispatch_keys']==[head['number']] and j['items'][1]['state']=='SKIPPED','单行恢复不重启另一张已取消任务')
        require(j['items'][0]['purchase_observation']['taskId']=='offline-original-task','恢复保留原平台任务号')
        store.update(uid,state='PAUSED');q.close()
        # A separately cloned synthetic DB exercises partial commit and exact
        # child deletion inside the deployed executable, without changing this
        # window's active workflow or any real data.
        partial_path=out/'partial.sqlite3'
        with closing(sqlite3.connect(path)) as source,closing(sqlite3.connect(partial_path)) as target:source.backup(target)
        partial=batches.preview_new(partial_path,[head,other],{'河北仓':catalog()})
        import threading
        stop=threading.Event()
        def stop_after_first(value):
            if getattr(value,'business_event',None):stop.set()
        r=batches.execute(partial_path,partial,progress=stop_after_first,cancel=stop)
        require(r['cancelled'] and len(r['generated'])==1,'实际EXE内部分分配后安全停止，已提交单保留')
        s=batches.recover_batch(partial_path,r['batch_id'])
        require(len(s['generated'])==1 and s['pending']==[other['number']],'部分成功恢复只标识未完成成员')
        continued=batches.continue_batch(partial_path,r['batch_id'])
        gids=r['generated']+continued['generated']
        require([orders.get(partial_path,g)['summary']['quantity'] for g in gids]==['40','60'],'部分成功后按原顺序和剩余库存继续，不重复占用')
        from aoxiang_workflows import Workflows
        from deletion_barrier import SourceDeleted,active
        local=Workflows(out/'partial-queue',business_path=partial_path);job=local.prepare([head]);job=local.claim(job['id']);item=job['items'][0]
        import data_maintenance
        deletion=data_maintenance.prepare(partial_path,[head]);data_maintenance.execute(partial_path,deletion,True)
        try:local.update(job['id'],item_id=item['item_id'],oid=head['oid'],execution_token=job['execution_token'],changes={'state':'BOUND'})
        except SourceDeleted:pass
        else:raise AssertionError('deleted source accepted a late result')
        with closing(sqlite3.connect(partial_path)) as db:
            require(not active(db,head['oid'],head['warehouse']) and active(db,sd['oid'],sd['warehouse']) and orders.active(db,other)==gids[1],'单仓删除与晚到结果不恢复已删仓，不影响另一仓和未选订单')
        # Real DB allocations, with a failure between register and first commit.
        p=batches.preview_new(path,[head],{'河北仓':catalog()})
        write('registration-scheduling.json',{'archive_reader_active':win.history_reader.active is not None,
              'home_reader_active':win.home_tasks.reader.active is not None,'uses_real_background_task':True})
        # Production invokes this service from a worker. A synchronous probe on
        # the GUI thread would prevent history IPC callbacks from completing.
        from business_ui import run
        run(win,batches.register,(path,p),'登记隔离验收原批次');bid=p['payload']['batch_id']
        new=batches.preview_new(path,[other],{'河北仓':catalog()});activate=batches.activate
        def fail(*a,**k):raise OSError('synthetic activation failure')
        batches.activate=fail
        try:
            try:batches.execute(path,new)
            except OSError:pass
        finally:batches.activate=activate
        with closing(sqlite3.connect(path)) as db:require(batches.context(db,bid)['status']=='active','首次分配前失败自动恢复原批运行状态')
        from jst_ui import GenerationDialog
        d=GenerationDialog(win,win,[head],bid);dialogs.append(d);d.show();QTest.qWait(80)
        d.plan=batches.preview_existing(path,bid);d.accept_issues.setChecked(True);d.generate_btn.setEnabled(True)
        begin=lc.begin;fired=[]
        def fail_final(db,cancel,**kw):
            if sys._getframe(1).f_code.co_name=='_execute' and not fired:
                fired.append(True);raise sqlite3.OperationalError('synthetic finalization error')
            return begin(db,cancel,**kw)
        lc.begin=fail_final
        try:
            QTest.mouseClick(d.generate_btn,Qt.MouseButton.LeftButton);wait(lambda:d.worker is None)
        finally:lc.begin=begin
        require(bool(fired) and d.receipt and d.receipt.get('finalization_pending'),'真实生成线程返回已保存及收尾待恢复回执')
        require(len(d.saved)==1 and '批次状态待恢复' in d.status.text(),'生成窗口明确显示已保存清单与恢复入口');shot(d,'allocation-finalization')
        QTest.mouseClick(d.recovery_btn,Qt.MouseButton.LeftButton)
        with closing(sqlite3.connect(path)) as db:
            require(batches.context(db,bid)['status']=='complete' and db.execute('SELECT count(*) FROM jst_generations').fetchone()[0]==1,'实际恢复按钮只补控制状态，不重复生成或预留')
        exported=__import__('jst_exports').export_checked(path,d.saved,out/'exports')
        require(len(exported['files'])==1,'恢复后的原生成单可正常导出');d.close()
        # Same real save/recovery UI for all four lifecycle facts.
        for stage,quantity in (('return',20),('operations',20),('payment',10),('receipt',8)):
            plan=plan_for(sd,stage,quantity,stage+'.xlsx');identity=interrupt(plan)
            dialog=RecoveryDialog(win,win,[sd],stage);dialogs.append(dialog);dialog.show();QTest.qWait(30)
            require(dialog.review is not None and dialog.review['action']=='retry' and dialog.go.isEnabled(),stage+' 当前页能恢复原未提交内容：'+dialog.note.text())
            if stage=='return':
                commit=lc._commit
                def cancellable(path,plan,progress,cancel):
                    from runtime_support import checkpoint
                    progress('离线验证：等待真实取消按钮')
                    if not cancel.wait(10):raise AssertionError('真实取消未到达')
                    checkpoint(cancel)
                lc._commit=cancellable;cancel_recovery[0]=True
                try:QTest.mouseClick(dialog.go,Qt.MouseButton.LeftButton)
                finally:lc._commit=commit;cancel_recovery[0]=False
                require(dialog.receipt is None and recovery.inspect(path,identity)['action']=='retry','恢复途中取消不写入业务，原预览仍可再次继续')
            QTest.mouseClick(dialog.go,Qt.MouseButton.LeftButton)
            require(bool(dialog.receipt),stage+' 实际恢复按钮经原校验保存完成')
            shot(dialog,'recovered-'+stage)
            before=lc.snapshot(path,sd)['documents'][stage]['version'];QTest.mouseClick(dialog.go,Qt.MouseButton.LeftButton)
            require(lc.snapshot(path,sd)['documents'][stage]['version']==before,stage+' 重复恢复不重复新增版本');dialog.close()
        # Changed upstream is a concrete review action, never a force-save.
        payment=plan_for(sd,'payment',11,'stale-payment.xlsx');identity=interrupt(payment)
        lc.commit(path,plan_for(sd,'return',21,'changed-return.xlsx'))
        before=lc.snapshot(path,sd)['documents']['payment']['version']
        try:recovery.recover(path,identity)
        except wr.Conflict:pass
        else:raise AssertionError('stale upstream was accepted')
        require(lc.snapshot(path,sd)['documents']['payment']['version']==before,'恢复仍拒绝已变化上游，保留原付款事实')
        # Recheck existing downstream through the unchanged lifecycle API.
        for stage in ('operations','payment','receipt'):lc.commit(path,lc.recheck(path,sd,stage))
        pending=plan_for(sd,'receipt',9,'reopen-receipt.xlsx');identity=interrupt(pending)
        write('reopen.json',{'checkpoint':identity,'head':sd,'bid':bid,'queue':uid})
        require(recovery.inspect(path,identity)['action']=='retry','退出前未完成保存材料已持久留存')
    def reopen():
        state=json.loads((out/'reopen.json').read_text('utf-8'));sd=state['head']
        dialog=RecoveryDialog(win,win,[sd],'receipt');dialogs.append(dialog);dialog.show();QTest.qWait(50)
        require(dialog.choices.currentData()==state['checkpoint'] and dialog.go.isEnabled(),'实际EXE重开后找到原未完成保存')
        QTest.mouseClick(dialog.go,Qt.MouseButton.LeftButton);require(bool(dialog.receipt),'重开后从当前恢复页完成原收货保存：'+dialog.note.text())
        before=lc.snapshot(path,sd)['documents']['receipt']['version'];QTest.mouseClick(dialog.go,Qt.MouseButton.LeftButton)
        require(lc.snapshot(path,sd)['documents']['receipt']['version']==before,'重开后再次恢复也不增加版本');shot(dialog,'reopen-completed');dialog.close()
        r=batches.recover_batch(path,state['bid'])
        require(len(r['generated'])==1 and not r['pending'],'重开核实原分配结果仍然唯一且可导出')
        job=manager.store.get(state['queue'])
        require(job['items'][0]['purchase_observation']['taskId']=='offline-original-task' and job['items'][1]['state']=='SKIPPED','重开保留原受理身份及取消范围')
    def close_recovery():
        state=json.loads((out/'reopen.json').read_text('utf-8'))
        dialog=RecoveryDialog(win,win,[state['head']],'receipt');dialogs.append(dialog);dialog.show();QTest.qWait(30)
        require(dialog.go.isEnabled(),'退出测试定位原未提交恢复任务')
        original=lc._commit
        def wait_for_close(path,plan,progress,cancel):
            from runtime_support import checkpoint
            progress('离线验证：恢复写入前等待安全关闭')
            if not cancel.wait(10):raise AssertionError('主窗口关闭没有安全取消恢复')
            checkpoint(cancel)
        lc._commit=wait_for_close
        try:
            QTimer.singleShot(100,win.close)
            QTest.mouseClick(dialog.go,Qt.MouseButton.LeftButton)
        finally:lc._commit=original
        require(win.closing and recovery.inspect(path,state['checkpoint'])['action']=='retry','恢复中正常关闭可安全退出，未提交材料留待重开，不再创建后续窗口')
    def work():
        try:
            {'exercise':exercise,'close_recovery':close_recovery,'reopen':reopen}[mode]()
            require(not unexpected,'没有忽略或替换真实错误弹窗')
            write(mode+'-report.json',{'passed':True,'checks':checks,'messages':messages,'synthetic':True,'live_platform':False,'dialogs_not_stubbed':True})
        except BaseException:
            write(mode+'-report.json',{'passed':False,'checks':checks,'error':traceback.format_exc(),'messages':messages})
        finally:
            timer.stop();manager.tick=old_tick;manager.stopping=True
            from shiboken6 import isValid
            for d in reversed(dialogs):
                if not isValid(d):continue
                if d.isVisible():d.close()
                d.deleteLater()
            QTimer.singleShot(100,win.close)
    QTimer.singleShot(150,work)
