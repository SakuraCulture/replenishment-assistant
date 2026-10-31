"""Opt-in app acceptance; uses only the launcher's generated QA database/files."""
import json,time,traceback,sqlite3,hashlib
from pathlib import Path
from contextlib import closing
from PySide6.QtCore import QTimer,QEventLoop,QMimeData,QUrl,QPointF,Qt
from PySide6.QtGui import QDropEvent
from PySide6.QtWidgets import QApplication,QMessageBox,QFileDialog,QInputDialog,QPushButton
import order_lifecycle as service
from version_info import diagnostic

def start_smoke(app,win,args):
    out=Path(args[0]);phase=args[1];state={};checks=[];shots=[];gaps=[];last=[time.perf_counter()]
    def wait(fn,seconds=45):
        loop=QEventLoop();timer=QTimer();start=time.monotonic();timer.timeout.connect(lambda:loop.quit() if fn() or time.monotonic()-start>seconds else None);timer.start(20);loop.exec();timer.stop()
        if not fn():raise AssertionError('UI wait timed out')
    def shot(w,name):w.grab().save(str(out/name));shots.append(name)
    def automate():
        from lifecycle_ui import Files,Mapping,Bindings,Review
        now=time.perf_counter();gaps.append(now-last[0]);last[0]=now;d=QApplication.activeModalWidget()
        if not d:return
        try:
            if isinstance(d,Files):
                mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(out/state['file']))]);event=QDropEvent(QPointF(20,20),Qt.DropAction.CopyAction,mime,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier);d.dropEvent(event)
                if d.paths!=[str((out/state['file']).resolve())]:raise AssertionError('drag/drop order differs')
                d.accept()
            elif isinstance(d,Mapping):
                if d.spec()['mapping']['商品编码']!=4:raise AssertionError('mapping differs')
                shot(d,'mapping-'+state['stage']+'.png');d.accept()
            elif isinstance(d,Bindings):
                for c in d.combos.values():
                    if c.currentData() is None:
                        if c.count()!=2:raise AssertionError('missing exact revision choice')
                        c.setCurrentIndex(1)
                if not d.go.isEnabled():raise AssertionError('binding remains disabled')
                d.accept()
            elif isinstance(d,Review):
                if d.save.isEnabled():raise AssertionError('whole-order confirmation default must not be checked')
                shot(d,'preview-'+state['stage']+'-'+state['mode']+'.png')
                if state['mode']=='cancel':d.reject()
                else:d.complete.setChecked(True);d.save.click()
            elif isinstance(d,QMessageBox):
                if d.windowTitle() in ('完整单据已保存','导出结果','模板已保存'):d.accept()
                elif d.windowTitle()=='未完成' and state.get('mode')=='invalid':state['invalid_seen']=d.text();d.accept()
                else:raise AssertionError(d.windowTitle()+': '+d.text())
        except Exception:
            state['automation_error']=traceback.format_exc();d.reject()
    timer=QTimer(win);timer.setInterval(35);timer.timeout.connect(automate);timer.start()
    def finish(error=None):
        timer.stop();report={'passed':error is None,'error':error,'checks':checks,'screenshots':shots,'version':diagnostic(),'phase':phase,'ui_ticks':len(gaps),'max_ui_gap_seconds':max(gaps,default=0)};(out/('ui-'+phase+'.json')).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');win.job_dirty=False;win._discard_close=True;win.close();app.exit(1 if error else 0)
    def exercise():
        try:
            from lifecycle_ui import import_document,LifecycleDialog,Document,export_selected,template
            wait(lambda:len(win.followup.records)==2);shot(win,'home-'+phase+'.png')
            if any(('暂缓' in b.text() or '恢复跟进' in b.text()) for b in win.findChildren(QPushButton)):raise AssertionError('manual follow-up button still exposed')
            if [b.text() for b in win.top_nav]!=['首页','工作页','订单管理']:raise AssertionError('top-level function grouping differs')
            checks.append('actual spawn home reader; four mutually exclusive document-derived states; no manual follow-up actions; grouped imports')
            path=win.work_repository.path
            if phase=='reopen':
                if not all(r['status']=='已收齐' for r in win.followup.records):raise AssertionError('home progress not restored')
                for h in win.followup.records:
                    v=service.snapshot(path,h)
                    if [v['documents'][s]['version'] for s in service.STAGES]!=[2,1,1]:raise AssertionError('versions not restored')
                    if v['rows'][0][3:6]!=[2,5,5]:raise AssertionError('independent quantities not restored')
                checks.append('second actual app process restores exact warehouse/revision versions and independent quantities');finish();return
            def original():
                with closing(sqlite3.connect(path)) as db:return {n:db.execute('SELECT * FROM '+n).fetchall() for n in ('normal_orders','normal_revisions','normal_work_revisions','source_files')}
            before=original();state.update(mode='cancel',file='return.xlsx',stage='return')
            if import_document(win,win,'return'):raise AssertionError('preview cancel saved')
            if service.history(path,win.followup.records[0],'return'):raise AssertionError('preview cancel created version')
            checks.append('actual multi-file drag/drop-compatible picker, field mapping, explicit missing revision binding, default-cancel complete-document preview')
            for stage,expected in [('payment','待回传'),('return','待收货')]:
                state.update(mode='save',file=stage+'.xlsx',stage=stage)
                if not import_document(win,win,stage):raise AssertionError(state.get('automation_error','UI save failed'))
                win.followup.query();wait(lambda:len(win.followup.records)==2 and all(r['status']==expected for r in win.followup.records))
            checks.append('payment arrives first and is retained; home still prompts return, then receipt; exact warehouse orders independent')
            state.update(mode='invalid',file='receipt-invalid.xlsx',stage='receipt')
            if import_document(win,win,'receipt') or not state.get('invalid_seen'):raise AssertionError('blank receipt quantity not rejected')
            state.update(mode='save',file='receipt.xlsx',stage='receipt')
            if not import_document(win,win,'receipt'):raise AssertionError('retry after invalid input failed')
            win.followup.query();wait(lambda:len(win.followup.records)==2 and all(r['status']=='已收齐' for r in win.followup.records));checks.append('invalid blank quantity rejected; subsequent valid receipt retry succeeds; four stages complete')
            state.update(mode='update',file='return-v2.xlsx',stage='return')
            if not import_document(win,win,'return'):raise AssertionError('return revision failed')
            state.update(mode='repeat')
            if not import_document(win,win,'return'):raise AssertionError('repeat import failed')
            for h in win.followup.records:
                if [v['version'] for v in service.history(path,h,'return')]!=[2,1]:raise AssertionError('repeat import increased version')
            if original()!=before:raise AssertionError('original signed facts changed')
            checks.append('whole-document revision replaces current quantities without accumulating, keeps prior version, repeated import idempotent; original four tables exact')
            h=win.followup.records[0];d=LifecycleDialog(win,win,h);d.show();shot(d,'lifecycle-comparison.png')
            if d.model.rows[0][3:6]!=[2,5,5]:raise AssertionError('comparison quantity columns differ')
            old_dir=QFileDialog.getExistingDirectory;old_item=QInputDialog.getItem;QFileDialog.getExistingDirectory=lambda *a,**k:str(out);QInputDialog.getItem=lambda *a,**k:('每仓一表多订单',True)
            for kind in ('comparison','payment','receipt'):
                result=export_selected(d,win,win.followup.records,kind)
                if len(result['files'])!=2 or result['error']:raise AssertionError('warehouse export failed')
            QInputDialog.getItem=old_item;QFileDialog.getExistingDirectory=old_dir;checks.append('actual export dialog writes comparison/payment/receipt; combined option keeps Shandong and Hebei separate')
            doc=Document(d,win,h,'return');doc.show();shot(doc,'document-versions.png');doc.versions.setCurrentIndex(1);source=doc.files.currentData();old_save=QFileDialog.getSaveFileName;dest=out/'original-return-v1.xlsx';QFileDialog.getSaveFileName=lambda *a,**k:(str(dest),'Excel');doc.download();QFileDialog.getSaveFileName=old_save
            if hashlib.sha256(dest.read_bytes()).hexdigest()!=source['sha']:raise AssertionError('downloaded original differs')
            doc.reject();doc.deleteLater();d.reject();d.deleteLater();checks.append('business form versions and source-line audit readable; exact original version downloaded through UI')
            for stage in service.STAGES:
                dest=out/('template-'+stage+'.xlsx');QFileDialog.getSaveFileName=lambda *a,**k:(str(dest),'Excel');template(win,stage)
                if not dest.exists():raise AssertionError('template button failed')
            QFileDialog.getSaveFileName=old_save;checks.append('three minimum templates downloaded through actual UI functions')
            win.followup.query();wait(lambda:len(win.followup.records)==2 and all(r['status']=='已收齐' for r in win.followup.records));shot(win,'home-complete.png')
            if state.get('automation_error'):raise AssertionError(state['automation_error'])
            if original()!=before:raise AssertionError('signed/source data changed')
            finish()
        except Exception:finish(traceback.format_exc())
    QTimer.singleShot(200,exercise)
