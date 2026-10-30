"""Opt-in native EXE maintenance acceptance, private synthetic settings only."""
import json,traceback,sys,hashlib
from pathlib import Path
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication,QMessageBox,QFileDialog,QInputDialog
import work_repository as wr
import normal_orders,storage_chunks as chunks
from ui_work_io import Progress
from version_info import diagnostic

def start_smoke(app,win,args):
    fixture,out=map(Path,args[:2]);out.mkdir(exist_ok=True);checks=[];shots=[]
    def finish(error=None):
        (out/'storage_ui_report.json').write_text(json.dumps({'passed':error is None,'error':error,'checks':checks,'screenshots':shots,'version':diagnostic()},ensure_ascii=False,indent=2),encoding='utf-8')
        win.job_dirty=False;win._discard_close=True;win.close();app.exit(0 if error is None else 1)
    sys.excepthook=lambda t,v,tb:finish(''.join(traceback.format_exception(t,v,tb)))
    def run():
        try:
            payload=wr.loads(fixture.read_text(encoding='utf-8'));payload['day']=wr.today()
            receipt=win.work_repository.save_job(payload['day'],payload)
            before=win.work_repository.get_job(payload['day'])
            import copy
            empty=win.snapshot();historical={**before,'payload':copy.deepcopy(empty)};historical['payload']['model_version']='0.3.0-rules'
            win.restore_job(historical,owned=True);assert win.readonly
            win.restore_job({**before,'payload':copy.deepcopy(empty)},owned=True);assert not win.readonly
            checks.append('old-model daily result remains readonly; current-model daily result remains editable')
            original_item,original_file,original_info=QInputDialog.getItem,QFileDialog.getSaveFileName,QMessageBox.information
            state={'mode':'legacy','cancel':False,'seen':False}
            QInputDialog.getItem=lambda *a,**k:('生成旧格式兼容副本' if state['mode']=='legacy' else '生成完整分块副本（校验并转换历史）',True)
            QFileDialog.getSaveFileName=lambda *a,**k:(str(out/('cancelled.sqlite3' if state['cancel'] else state['mode']+'.sqlite3')),'')
            QMessageBox.information=lambda *a,**k:QMessageBox.StandardButton.Ok
            timer=QTimer(win);timer.setInterval(20)
            def observe():
                box=QApplication.activeModalWidget()
                if isinstance(box,Progress) and box.operation=='storage_copy':
                    if not state['seen']:
                        name='storage-'+state['mode']+('-cancel' if state['cancel'] else '')+'.png';box.grab().save(str(out/name));shots.append(name);state['seen']=True
                    if state['cancel']:box.reject()
            timer.timeout.connect(observe);timer.start()
            try:
                win.storage_copy();assert (out/'legacy.sqlite3').is_file()
                import sqlite3,zlib
                with sqlite3.connect(out/'legacy.sqlite3') as db:
                    raw=db.execute('SELECT payload FROM jobs').fetchone()[0];assert wr.dumps(wr.loads(zlib.decompress(raw)))==wr.dumps(payload)
                checks.append('actual home maintenance action creates legacy-compatible exact snapshot in independent path')
                state.update(mode='upgrade',seen=False);win.storage_copy();assert (out/'upgrade.sqlite3').is_file()
                with sqlite3.connect(out/'upgrade.sqlite3') as db:
                    raw=db.execute('SELECT payload FROM jobs').fetchone()[0];assert chunks.manifest(raw) is not None;assert wr.dumps(chunks.load(raw,db))==wr.dumps(payload)
                checks.append('actual UI upgrade-copy roundtrip retains full values, IDs and snapshot digest')
                state.update(cancel=True,seen=False);win.storage_copy();assert not (out/'cancelled.sqlite3').exists()
                assert not list(out.glob('*.tmp'));checks.append('cooperative cancellation waits for worker cleanup and publishes no file')
                state.update(cancel=False,mode='legacy');errors=[];original_failed=win.failed;win.failed=errors.append
                win.storage_copy();win.failed=original_failed;assert errors and '已存在' in errors[0];checks.append('existing destination refused without overwriting')
            finally:
                timer.stop();QInputDialog.getItem=original_item;QFileDialog.getSaveFileName=original_file;QMessageBox.information=original_info
            assert win.work_repository.get_job(payload['day'])==before;checks.append('all maintenance actions preserve current live job revision and complete payload')
            finish()
        except Exception:finish(traceback.format_exc())
    QTimer.singleShot(350,run)
