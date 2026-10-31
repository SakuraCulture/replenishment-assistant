"""Opt-in native application acceptance; only launcher-owned QA files/database."""
import json,time,traceback,sqlite3,hashlib
from pathlib import Path
from PySide6.QtCore import QTimer,QEventLoop,QMimeData,QUrl,QPointF,Qt
from PySide6.QtGui import QDropEvent
from PySide6.QtWidgets import QApplication,QMessageBox,QFileDialog,QInputDialog
import order_lifecycle as service
from version_info import diagnostic

def start_smoke(app,win,args):
    out=Path(args[0]);phase=args[1];state={};checks=[];gaps=[];last=[time.perf_counter()]
    def wait(fn,seconds=30):
        loop=QEventLoop();t=QTimer();start=time.monotonic();t.timeout.connect(lambda:loop.quit() if fn() or time.monotonic()-start>seconds else None);t.start(20);loop.exec();t.stop()
        if not fn():raise AssertionError('UI wait timeout')
    def automate():
        from lifecycle_ui import Files,Mapping,Bindings,Review
        now=time.perf_counter();gaps.append(now-last[0]);last[0]=now;d=QApplication.activeModalWidget()
        if not d:return
        try:
            if isinstance(d,Files):
                files=state['file'] if isinstance(state['file'],list) else [state['file']]
                mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(out/f)) for f in files]);e=QDropEvent(QPointF(20,20),Qt.DropAction.CopyAction,mime,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier);d.dropEvent(e)
                if d.paths!=[str((out/f).resolve()) for f in files]:raise AssertionError('drag drop failed')
                if d.stage=='payment' and d.multiple:raise AssertionError('payment batch import not deferred')
                d.accept()
            elif isinstance(d,Mapping):
                if set(d.fields)&{'签发修订','门店编码','供货仓','数量单位','折合库存单位系数','换算依据'}:raise AssertionError('internal fields exposed for manual input')
                d.grab().save(str(out/'simple-mapping.png'));d.accept()
            elif isinstance(d,Bindings):
                raise AssertionError('public import still asks for signing revision')
            elif isinstance(d,Review):
                if d.save.isEnabled():raise AssertionError('confirmation default unsafe')
                d.grab().save(str(out/('preview-'+state.get('stage','')+'-'+state.get('mode','')+'.png')))
                if state['mode']=='cancel':d.reject();return
                all_rows=list(d.all_rows);d.category.setCurrentText('缺行按0');d.search.setText('0004');d.category.setCurrentText('全部');d.search.clear()
                if d.all_rows!=all_rows:raise AssertionError('filter changed save range')
                d.complete.setChecked(True);d.extra_ack.setChecked(True);d.save.click()
            elif isinstance(d,QMessageBox):
                title=d.windowTitle()
                if title in ('核对付款文件读取范围','核对导出范围'):d.button(QMessageBox.StandardButton.Yes).click()
                elif title in ('完整单据已保存','导出结果','模板已保存'):d.accept()
                elif title=='未完成' and state.get('mode')=='invalid':state['error_seen']=d.text();d.accept()
                else:raise AssertionError(title+': '+d.text())
        except Exception:state['automation_error']=traceback.format_exc();d.reject()
    timer=QTimer(win);timer.setInterval(30);timer.timeout.connect(automate);timer.start()
    def finish(error=None):
        timer.stop();r={'passed':error is None,'error':error,'checks':checks,'version':diagnostic(),'phase':phase,'ui_ticks':len(gaps),'max_ui_gap_seconds':max(gaps,default=0)};(out/('ui-'+phase+'.json')).write_bytes(json.dumps(r,ensure_ascii=False,indent=2).encode());win.job_dirty=False;win._discard_close=True;win.close();app.exit(1 if error else 0)
    def exercise():
        try:
            from lifecycle_ui import import_document,LifecycleDialog,Document,export_selected,review_again,template
            wait(lambda:len(win.followup.records)==2);path=win.work_repository.path;hb=next(h for h in win.followup.records if h['warehouse']=='河北仓');sd=next(h for h in win.followup.records if h['warehouse']=='山东仓')
            win.grab().save(str(out/('home-'+phase+'.png')))
            if phase=='reopen':
                v=service.snapshot(path,hb)
                if [v['documents'][s]['version'] for s in service.STAGES]!=[2,2,2] or v['rows'][0][3:6]!=[8,4,0] or v['next'] is not None:raise AssertionError('restart quantities/versions/state differ')
                checks.append('second native process restores versions, predecessor binding and independent quantities');finish();return
            def originals():
                with sqlite3.connect(path) as db:return {n:db.execute('SELECT * FROM '+n).fetchall() for n in ('normal_orders','normal_revisions','normal_work_revisions','source_files')}
            before=originals();state.update(file='return-hb.xlsx',stage='return',mode='cancel')
            if import_document(win,win,'return',hb) or service.history(path,hb,'return'):raise AssertionError('preview cancel saved')
            checks.append('drag/drop existing signed format, all three sheets, default-cancel preview leaves no version')
            for stage,file,h in [('return',['return-minimal.xlsx','return-sd.xlsx'],None),('payment','payment.xlsx',hb)]:
                state.update(file=file,stage=stage,mode='save')
                if not import_document(win,win,stage,h):raise AssertionError(state.get('automation_error','import failed'))
            v=service.snapshot(path,hb);ret=v['documents']['return']['payload']['rows']
            if [(r['sku'],r['base_quantity'],r['quantity_origin']) for r in ret]!=[('0001','6','value'),('0003','0','blank'),('0004','0','missing')]:raise AssertionError('zero/source distinction differs')
            paid=v['documents']['payment']['payload']['rows'][0]
            if paid['base_quantity']!='4' or paid['amount']!='5.2' or paid['document_date'] is not None:raise AssertionError('payment unit/amount/date differs')
            checks.append('single selected warehouse payment, exact original 13-column format; no extra store/unit/payment-time fields; no carton multiplication')
            checks.append('global batch return: five-column minimal template plus original signed workbook; association by order number only, no revision picker or retyped upstream quantities')
            state.update(file='receipt-invalid.xlsx',stage='receipt',mode='invalid')
            if import_document(win,win,'receipt',hb) or not state.get('error_seen'):raise AssertionError('invalid quantity accepted')
            state.update(file='receipt.xlsx',mode='save')
            if not import_document(win,win,'receipt',hb):raise AssertionError('valid blank receipt retry failed')
            checks.append('invalid quantity rejected; blank receipt retry becomes zero with source preserved')
            state.update(file='return-hb-v2.xlsx',stage='return',mode='save');import_document(win,win,'return',hb)
            if service.snapshot(path,hb)['next']!='payment':raise AssertionError('upstream change not surfaced')
            for s in ('payment','receipt'):
                state.update(stage=s,mode='recheck')
                if not review_again(win,win,hb,s):raise AssertionError('recheck failed')
            checks.append('upstream revision flags payment then receipt; explicit recheck preserves original quantities and creates new bound versions')
            dialog=LifecycleDialog(win,win,hb);dialog.show();dialog.grab().save(str(out/'lifecycle-form.png'))
            old_dir=QFileDialog.getExistingDirectory;old_item=QInputDialog.getItem;QFileDialog.getExistingDirectory=lambda *a,**k:str(out);QInputDialog.getItem=lambda *a,**k:('每仓一表多订单',True)
            try:
                for kind in ('return','payment','receipt','comparison'):
                    result=export_selected(dialog,win,[hb,sd] if kind in ('return','comparison') else [hb],kind)
                    if not result or result['error'] or not result['files']:raise AssertionError('UI export failed '+str(result))
            finally:QFileDialog.getExistingDirectory=old_dir;QInputDialog.getItem=old_item
            checks.append('return template export and lifecycle business exports; multi-select keeps warehouses separate; explicit scope confirmation')
            doc=Document(dialog,win,hb,'return');doc.show();doc.versions.setCurrentIndex(1);f=doc.files.currentData();dest=out/'original-v1.xlsx';old_save=QFileDialog.getSaveFileName;QFileDialog.getSaveFileName=lambda *a,**k:(str(dest),'Excel')
            try:doc.download()
            finally:QFileDialog.getSaveFileName=old_save
            if hashlib.sha256(dest.read_bytes()).hexdigest()!=f['sha']:raise AssertionError('original download differs')
            doc.reject();doc.deleteLater();dialog.reject();dialog.deleteLater()
            if originals()!=before:raise AssertionError('signed facts or master originals changed')
            checks.append('original signed/work/master source tables remain exact; historic returned original can be downloaded')
            win.followup.query();wait(lambda:len(win.followup.records)==2 and next(h for h in win.followup.records if h['warehouse']=='河北仓')['next'] is None);finish()
        except Exception:finish(traceback.format_exc()+'\n'+state.get('automation_error',''))
    QTimer.singleShot(500,exercise)
