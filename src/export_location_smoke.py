"""Opt-in test of the real settings form and dialog defaults; no user data."""
import json,traceback,sqlite3,time
from contextlib import closing
from pathlib import Path
from PySide6.QtCore import QTimer,QEventLoop,QStandardPaths
from PySide6.QtWidgets import QApplication,QPushButton,QFileDialog,QMessageBox,QDialog,QInputDialog
import export_locations as locations,export_settings as settings,purchases
from version_info import diagnostic

def start_smoke(app,win,args):
    out=Path(args[0]);phase=args[1];checks=[];shots=[];state={};target=out/'默认导出 文件夹';target.mkdir(exist_ok=True);temporary=out/'仅本次';temporary.mkdir(exist_ok=True);config=locations.config_path(win)
    def wait(fn):
        loop=QEventLoop();timer=QTimer();start=time.monotonic();timer.timeout.connect(lambda:loop.quit() if fn() or time.monotonic()-start>30 else None);timer.start(20);loop.exec();timer.stop()
        if not fn():raise AssertionError('UI wait timed out')
    def shot(w,name):w.grab().save(str(out/name));shots.append(name)
    def automate():
        d=QApplication.activeModalWidget()
        if isinstance(d,locations.ExportSettingsDialog):
            try:
                if Path(d.location.text())!=config:raise AssertionError('configuration path does not match active data directory')
                if state['mode']=='reopen':
                    if d.directory.text()!=str(target.resolve()):raise AssertionError('persisted preference was not loaded')
                    shot(d,'settings-reopen.png');d.reject();return
                d.browse()
                if d.directory.text()!=str(target):raise AssertionError('browse did not populate chosen folder')
                shot(d,'settings-'+state['mode']+'.png')
                if state['mode']=='cancel':d.reject()
                else:d.save_btn.click()
            except Exception:state['error']=traceback.format_exc();d.reject()
        elif isinstance(d,QMessageBox):
            if d.windowTitle() in ('默认导出位置暂不可用','模板已保存','导出完成','导出结果'):state.setdefault('messages',[]).append(d.windowTitle());d.accept()
            else:state['error']=d.windowTitle()+': '+d.text();d.reject()
    timer=QTimer(win);timer.setInterval(30);timer.timeout.connect(automate);timer.start()
    def finish(error=None):
        timer.stop();(out/('ui-'+phase+'.json')).write_text(json.dumps({'passed':error is None,'error':error,'phase':phase,'checks':checks,'screenshots':shots,'version':diagnostic()},ensure_ascii=False,indent=2),encoding='utf-8');win.job_dirty=False;win._discard_close=True;win.close();app.exit(1 if error else 0)
    def exercise():
        old_dir=QFileDialog.getExistingDirectory;old_save=QFileDialog.getSaveFileName;old_item=QInputDialog.getItem
        try:
            wait(lambda:len(win.followup.records)==2)
            button=next(b for b in win.findChildren(QPushButton) if b.text()=='导出设置')
            if config!=out/'settings'/settings.FILENAME:raise AssertionError('preference escaped QA settings directory')
            QFileDialog.getExistingDirectory=lambda *a,**k:str(target)
            if phase=='reopen':
                state['mode']='reopen';button.click()
                if state.get('error'):raise AssertionError(state['error'])
                if locations.initial_directory(win)!=str(target.resolve()):raise AssertionError('restart export default differs')
                checks.append('second real process loads persisted default, opens settings form with exact configuration path; no user settings accessed');finish();return
            with closing(sqlite3.connect(win.work_repository.path)) as db:before={n:db.execute('SELECT * FROM '+n).fetchall() for n in ('normal_orders','normal_revisions','normal_work_revisions','source_files')}
            state['mode']='cancel';button.click()
            if config.exists():raise AssertionError('cancel wrote a configuration file')
            checks.append('actual top navigation opens settings; folder selection then Cancel creates no configuration')
            state['mode']='save';button.click()
            if settings.load(config)!=str(target.resolve()):raise AssertionError('Save did not persist directory')
            raw=config.read_bytes();checks.append('actual settings Save persists UTF-8 path atomically in active local configuration folder')
            calls=[]
            def choose_dir(*a,**k):
                calls.append(a)
                if Path(a[2])!=target:raise AssertionError('folder picker did not start in configured directory')
                return str(temporary)
            QFileDialog.getExistingDirectory=choose_dir
            if locations.directory(win,'本次另选')!=str(temporary) or config.read_bytes()!=raw:raise AssertionError('temporary directory changed preference')
            def cancel_save(*a,**k):
                if Path(a[2])!=target/'原文件名.xlsx' or k['options']!=QFileDialog.Option.DontConfirmOverwrite:raise AssertionError('filename or native options changed')
                return '',''
            QFileDialog.getSaveFileName=cancel_save;nested=QDialog(win)
            if locations.save_file(nested,'取消导出','原文件名.xlsx','Excel (*.xlsx)',options=QFileDialog.Option.DontConfirmOverwrite)!=('',''):raise AssertionError('cancel result changed')
            nested.deleteLater()
            if config.read_bytes()!=raw:raise AssertionError('cancelled export changed preference')
            checks.append('temporary directory and cancelled nested save dialog preserve preference; basename/filter/options forwarded unchanged')
            def save_default(*a,**k):
                if Path(a[2]).parent!=target:raise AssertionError('save dialog default directory differs')
                return a[2],a[3]
            QFileDialog.getSaveFileName=save_default
            import import_ui
            import_ui.download(win,'feedback')
            if not list(target.glob('*模板.xlsx')):raise AssertionError('template did not reach default directory')
            from lifecycle_ui import export_selected
            QFileDialog.getExistingDirectory=lambda *a,**k:str(target) if Path(a[2])==target else (_ for _ in ()).throw(AssertionError('batch picker ignored default'))
            QInputDialog.getItem=lambda *a,**k:('每仓一表多订单',True)
            result=export_selected(win,win,win.followup.records)
            if not result or result['error'] or len(result['files'])!=2 or any(Path(p).parent!=target for p in result['files']):raise AssertionError('actual batch export did not use chosen default')
            checks.append('actual template download and lifecycle batch export callbacks write three workbooks into configured directory, warehouses separate')
            gone=out/'目录已被移除';gone.mkdir();settings.save(config,str(gone));saved=config.read_bytes();gone.rmdir()
            if locations.initial_directory(win)!=QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation):raise AssertionError('invalid directory fallback differs')
            if config.read_bytes()!=saved or '默认导出位置暂不可用' not in state.get('messages',[]):raise AssertionError('invalid folder silently altered preference')
            settings.save(config,str(target));checks.append('deleted default folder produces explicit warning; falls back for current dialog without rewriting saved setting')
            with closing(sqlite3.connect(win.work_repository.path)) as db:after={n:db.execute('SELECT * FROM '+n).fetchall() for n in before}
            if before!=after:raise AssertionError('export settings altered archive business data')
            if state.get('error'):raise AssertionError(state['error'])
            checks.append('signed orders, revisions, work snapshots and original source blobs unchanged');finish()
        except Exception:finish(traceback.format_exc())
        finally:QFileDialog.getExistingDirectory=old_dir;QFileDialog.getSaveFileName=old_save;QInputDialog.getItem=old_item
    QTimer.singleShot(200,exercise)
