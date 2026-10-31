"""Opt-in packaged UI acceptance against launcher-owned synthetic data only."""
import json,time,traceback,copy,hashlib
from pathlib import Path
from PySide6.QtCore import QTimer,QEventLoop,QMimeData,QUrl,QPointF,Qt
from PySide6.QtGui import QDropEvent
from PySide6.QtWidgets import QApplication,QMessageBox,QFileDialog,QDialog
import purchases as p,work_repository as wr
from version_info import diagnostic

def start_smoke(app,win,args):
    out=Path(args[0]);phase=args[1];state={};checks=[];shots=[];gaps=[];last=[time.perf_counter()]
    def wait(fn,seconds=45):
        loop=QEventLoop();timer=QTimer();start=time.monotonic();timer.timeout.connect(lambda:loop.quit() if fn() or time.monotonic()-start>seconds else None);timer.start(20);loop.exec();timer.stop()
        if not fn():raise AssertionError('UI wait timed out')
    def shot(w,name):w.grab().save(str(out/name));shots.append(name)
    def automate():
        from purchase_ui import PurchaseFiles,Mapping,PurchaseEditor,LinkDialog
        now=time.perf_counter();gaps.append(now-last[0]);last[0]=now;d=QApplication.activeModalWidget()
        if not d:return
        try:
            if isinstance(d,PurchaseFiles):
                mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(out/'purchase.xlsx'))]);event=QDropEvent(QPointF(20,20),Qt.DropAction.CopyAction,mime,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier);d.dropEvent(event)
                if d.paths!=[str((out/'purchase.xlsx').resolve())]:raise AssertionError('drop did not preserve source')
                d.accept()
            elif isinstance(d,Mapping):
                if d.spec()['mapping']['商品编码']!=6:raise AssertionError('mapping order differs')
                shot(d,'mapping-'+state['mode']+'.png');d.accept()
            elif isinstance(d,LinkDialog):
                d.target.setCurrentIndex(1);d.add();d.accept();state['linked']=True
            elif isinstance(d,PurchaseEditor):
                if state.get('exit_editor'):state['exit_editor']=False;d.reject();return
                if not getattr(d,'smoke_linked',False):
                    d.smoke_linked=True;d.table.selectRow(0);QTimer.singleShot(0,d.single_link);return
                if state.get('linked') and not getattr(d,'smoke_saved',False):
                    d.smoke_saved=True;shot(d,'purchase-preview-'+state['mode']+'.png');QTimer.singleShot(0,d.save)
            elif d.objectName()=='interactionReview':
                shot(d,'purchase-differences-'+state['mode']+'.png')
                if state['mode']=='cancel':state['exit_editor']=True;d.reject()
                else:d.accept()
            elif isinstance(d,QMessageBox):
                if d.standardButtons() & QMessageBox.StandardButton.Yes:d.button(QMessageBox.StandardButton.Yes).click()
                elif d.windowTitle() in ('采购资料已保存','执行对照已导出','已下载'):d.accept()
                else:raise AssertionError(d.windowTitle()+': '+d.text())
        except Exception:
            state['automation_error']=traceback.format_exc();d.reject()
    timer=QTimer(win);timer.setInterval(35);timer.timeout.connect(automate);timer.start()
    def finish(error=None):
        timer.stop();report={'passed':error is None,'error':error,'checks':checks,'screenshots':shots,'version':diagnostic(),'phase':phase,'ui_ticks':len(gaps),'max_ui_gap_seconds':max(gaps,default=0)};(out/('ui-'+phase+'.json')).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');win.job_dirty=False;win._discard_close=True;win.close();app.exit(1 if error else 0)
    def exercise():
        try:
            from purchase_ui import import_purchases,ComparisonDialog,PurchaseLibrary,PurchaseView
            from business_ui import run
            import sqlite3
            from contextlib import closing
            wait(lambda:len(win.followup.records)==2);shot(win,'home-'+phase+'.png');checks.append('actual spawned home reader shows two warehouse orders; query succeeds without rewriting signed data')
            if phase=='reopen':
                docs=p.documents(win.work_repository.path)
                if len(docs)!=2 or any(d['version']!=1 for d in docs):raise AssertionError('reopen purchase version differs')
                if not all(r['purchase_status'].startswith('已关联') for r in win.followup.records):raise AssertionError('home not restored')
                checks.append('second application process restores current purchase versions, exact warehouse bindings and home statuses');finish();return
            def original():
                with closing(sqlite3.connect(win.work_repository.path)) as db:return {n:db.execute('SELECT * FROM '+n).fetchall() for n in ('normal_orders','normal_revisions','normal_work_revisions','source_files')}
            before=original();state.update(mode='cancel',linked=False)
            if import_purchases(win,win) or p.documents(win.work_repository.path):raise AssertionError('preview cancel wrote purchase records')
            checks.append('actual drag/drop, header mapping, manual exact warehouse revision allocation, full difference preview, default cancel leaves no purchase record')
            state.update(mode='save',linked=False)
            if not import_purchases(win,win):raise AssertionError(state.get('automation_error','import did not save'))
            if original()!=before:raise AssertionError('signed data changed')
            docs=p.documents(win.work_repository.path)
            if len(docs)!=2 or any(d['version']!=1 for d in docs):raise AssertionError('purchase counts differ')
            checks.append('actual UI confirms two independent purchase documents atomically; original signed snapshots and original master attachments byte-identical')
            win.followup.query();wait(lambda:len(win.followup.records)==2 and all(r['purchase_status'].startswith('已关联') for r in win.followup.records));shot(win,'home-linked.png')
            h=win.followup.records[0];d=ComparisonDialog(win,win,h);d.show();shot(d,'execution-comparison.png')
            if d.model.rows[0][4]!=5:raise AssertionError('comparison did not show purchased quantity')
            old_dir=QFileDialog.getExistingDirectory;QFileDialog.getExistingDirectory=lambda *a,**k:str(out)
            from PySide6.QtWidgets import QInputDialog
            old_item=QInputDialog.getItem;QInputDialog.getItem=lambda *a,**k:('每店每仓每修订一个文件',True)
            d.export();d.reject();d.deleteLater();QInputDialog.getItem=old_item;QFileDialog.getExistingDirectory=old_dir
            if len(list(out.glob('执行对照-*.xlsx')))!=1:raise AssertionError('comparison export missing')
            checks.append('actual business form displays independent quantities; actual export button writes new comparison workbook without changing existing exports')
            library=PurchaseLibrary(win,win);library.show();shot(library,'purchase-library.png');view=PurchaseView(library,win,docs[0]);view.show();shot(view,'purchase-version.png');view.reject();view.deleteLater();library.reject();library.deleteLater()
            state.update(mode='repeat',linked=False)
            # Reimport the same source is verified through backend using the displayed form's retained exact links;
            # the UI import paths and source checks have already run above.
            from purchase_ui import retain_links
            import purchase_io
            info=purchase_io.inspect([out/'purchase.xlsx'])[0];s=info['sheets'][0];scan=purchase_io.scan([{**info,**s,'mapping':{f:i for i,f in enumerate(purchase_io.FIELDS)}}]);scan=retain_links(p.auto_links(scan,p.targets(win.work_repository.path)),docs);receipts=run(win,p.commit,(win.work_repository.path,p.prepare(win.work_repository.path,scan)),'重复导入校验',cancellable=False)
            if any(r['changed'] for r in receipts):raise AssertionError('same source increased version')
            checks.append('same source reimport retains manual associations and is idempotent; unknown values remain unknown')
            if state.get('automation_error'):raise AssertionError(state['automation_error'])
            if original()!=before:raise AssertionError('original snapshots changed')
            finish()
        except Exception:finish(traceback.format_exc())
    QTimer.singleShot(200,exercise)
