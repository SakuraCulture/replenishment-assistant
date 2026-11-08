"""Closed synthetic Qt/sign/export acceptance; never a live platform operation."""
import copy,json,time,traceback
from pathlib import Path
from PySide6.QtCore import QTimer,Qt
from PySide6.QtTest import QTest
from version_info import diagnostic,MODEL_VERSION


def start_smoke(app,win,args):
    out=Path(args[0]);out.mkdir(parents=True,exist_ok=True);checks=[];report={};dialogs=[]
    def require(ok,text):
        if not ok:raise AssertionError(text)
        checks.append(text)
    def wait(predicate):
        until=time.monotonic()+20
        while not predicate() and time.monotonic()<until:QTest.qWait(15)
        require(predicate(),'Background task completed')
    def exercise():
        nonlocal report
        try:
            import result_semantics as sem,preissue,normal_delivery,normal_orders,export_receipts,order_identity
            from preissue_ui import FinalListDialog
            from final_list_filter_ui import FilterDialog
            from final_list_bulk_ui import BulkDialog
            from readable_ui import failure_message
            def row(store,sku,step,qty,cat,week,month):
                return dict(B=store,E=sku,D='合成商品'+sku,AU='山东仓',G='包',BD=step,V=2,S='启用',H=1,I=week,J=month,K=0,L=0,N=0,
                    U='快销',_category=cat,AN=2,AM=1,BH=10,BN=10,AS=qty,BV=qty*2,_source='资料匹配',_meta={'blocking':[],'notes':[]},_audit={},
                    _preview=False,_candidate_qty=qty,_candidate_amount=qty*2,_must_sell=False)
            win.results={}
            for s in ['QA1','QA2']:
                rows=[row(s,'drink',24,24,'饮料',10,30),row(s,'food',12,12,'食品',1,5),row(s,'missing',1,0,None,0,None)]
                win.results[s]={'rows':rows,'store':s,'store_name':'隔离门店'+s,'config':{'parameters':{'cap_days':21}},'budget':{'enabled':False},'summary':sem.summarize(rows),'sources':{'fixture':'synthetic'}}
            win.states={s:{'state':'已完成','name':r['store_name']} for s,r in win.results.items()};win.preissue_drafts={};win.input_dirty=False
            before=copy.deepcopy(win.results);d=FinalListDialog(win,list(win.results));dialogs.append(d);d.show();QTest.qWait(100)
            d.check_visible(True)
            modal_errors=[]
            def configure_filters():
                f=app.activeModalWidget()
                if not isinstance(f,FilterDialog):QTimer.singleShot(20,configure_filters);return
                try:
                    w=f.lists['categories']
                    for i in range(w.count()):w.item(i).setCheckState(Qt.CheckState.Checked if w.item(i).text()=='饮料' else Qt.CheckState.Unchecked)
                    op,low,high=f.numbers['month'];op.setCurrentIndex(op.findData('ge'));low.setText('30')
                    op,low,high=f.numbers['available'];op.setCurrentIndex(op.findData('le'));low.setText('0')
                    f.grab().save(str(out/'01-filter-editor.png'));f.accept()
                except Exception:modal_errors.append(traceback.format_exc());f.reject()
            QTimer.singleShot(20,configure_filters);d.edit_business_filters()
            require(not modal_errors and d.proxy.rowCount()==2,'Actual category/month-sales/stock filter matches two stores; missing sales excluded')
            require(len(d.model.checked)==6 and len(d.chosen())==2 and '筛选外4款' in d.metrics.text(),'Hidden checked products preserved and default batch scope excludes them')
            d.grab().save(str(out/'02-filtered-final-list.png'))
            def configure_bulk(cancel=False):
                b=app.activeModalWidget()
                if not isinstance(b,BulkDialog):QTimer.singleShot(20,lambda:configure_bulk(cancel));return
                try:
                    b.reason.setText('隔离验收：按动销补货');b.amount.setText('1');b.preview()
                    require(b.prepared is not None and b.table.model().rowCount()==2,'Batch dialog previews every selected row and its amount change')
                    b.grab().save(str(out/('03-batch-cancel.png' if cancel else '04-batch-preview.png')))
                    if cancel:b.reject()
                    else:b.fill.click()
                except Exception:modal_errors.append(traceback.format_exc());b.reject()
            QTimer.singleShot(20,lambda:configure_bulk(True));d.bulk();require(not d.model.edits['QA1'] and win.results==before,'Cancelling batch preview leaves edits and original results unchanged')
            QTimer.singleShot(20,configure_bulk);d.bulk();require(not modal_errors,'Actual batch modal completes without errors')
            require(all(d.model.edits[s]['drink']['quantity']==48 and len(d.model.edits[s])==1 for s in win.results),'Batch adds each product own minimum pack, writes reason, and protects hidden products')
            d.preview();wait(lambda:d.task is None);require(d.prepared is not None,'Final budget/risk review succeeds after batch reason assignment')
            d.confirm_all.setChecked(True);require(d.apply_btn.isEnabled(),'Apply enabled only after successful review and risk acknowledgement')
            d.apply_btn.click();wait(lambda:d.task is None);require(d.outcome['action']=='apply','Actual apply produces final results')
            win.results=d.outcome['results'];d.deleteLater();dialogs.clear()
            payload={'schema':1,'day':win.job_day,'model_version':MODEL_VERSION,'input_dirty':False,'new_focus':None,'states':win.states,'results':win.results}
            blocked=copy.deepcopy(payload);blocked['preissue_drafts']={'QA1':{'status':'待核对'}}
            repository=normal_orders.Repository(win.work_repository);old_receipts=export_receipts.recent(win.work_repository.path)
            try:normal_delivery.prepare_batch(repository,normal_delivery.probe_payload(blocked,['QA1']),['QA1']);raise AssertionError('Pending draft passed')
            except ValueError as error:require('待核对' in str(error),'Real projected export preflight blocks pending adjustment before signing')
            require(repository.headers()==[] and export_receipts.recent(win.work_repository.path)==old_receipts,'Blocked export creates neither signed order nor misleading export receipt')
            require('关闭占用文件' not in failure_message('门店尚有待核对的签发前调整：QA1'),'Pending-adjustment message does not advise closing Excel')
            from PySide6.QtWidgets import QMessageBox
            win.preissue_drafts={'QA1':{'status':'待核对','edits':{}}};messages=[]
            controller=QTimer(win);controller.setInterval(30)
            def close_message():
                modal=app.activeModalWidget()
                if isinstance(modal,QMessageBox):
                    messages.append(modal.text()+'\n'+modal.informativeText());modal.grab().save(str(out/'05-pending-export-blocked.png'));modal.reject()
            controller.timeout.connect(close_message);controller.start()
            try:win.export_current(['QA1'])
            finally:controller.stop();win.preissue_drafts={}
            (out/'main-entry-messages.json').write_text(json.dumps(messages,ensure_ascii=False,indent=2),'utf-8')
            require(bool(messages) and '应用最终数量' in messages[-1] and '关闭占用文件' not in messages[-1],'Actual main-window sign/export entry stops at preflight with actionable pending-adjustment message')
            require(repository.headers()==[] and export_receipts.recent(win.work_repository.path)==old_receipts,'Actual blocked GUI export creates no signed revision, file or export receipt')
            exports=out/'exports';exports.mkdir();batch=normal_delivery.prepare_batch(repository,payload,['QA1','QA2']);done=normal_delivery.execute_batch(repository,batch,exports)
            require(len(done['paths'])==2 and all(Path(p).is_file() for p in done['paths']),'Reviewed batch signs and exports both isolated stores')
            import openpyxl
            for path in done['paths']:
                book=openpyxl.load_workbook(path,read_only=True)
                try:require(bool(order_identity.read(book)),'Export retains hidden signed identity')
                finally:book.close()
            report={'passed':True,**diagnostic(),'checks':checks,'synthetic':True,'live_platform':False}
        except Exception:report={'passed':False,'checks':checks,'error':traceback.format_exc()}
        finally:
            for d in dialogs:d.dirty=False;d.reject();d.deleteLater()
            (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
            win.results={};win.preissue_drafts={};win.job_dirty=False;win._discard_close=True;win.close();QTimer.singleShot(150,lambda:app.exit(0 if report.get('passed') else 1))
    QTimer.singleShot(150,exercise)
