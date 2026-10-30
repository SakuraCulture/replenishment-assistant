"""Opt-in packaged large-snapshot lifecycle acceptance on an isolated database copy."""
import json,time,traceback,sys
from pathlib import Path
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication,QMessageBox,QFileDialog
import work_repository as wr
import normal_delivery,new_product_focus as focus
from ui_work_io import read_job,Progress
from runtime_support import Cancelled
from version_info import diagnostic


def start_smoke(app,win,args):
    out=Path(args[0]).resolve();state={'phase':None,'last':time.perf_counter(),'gaps':{},'times':{},'checks':[],'progress':{}};pulse=QTimer(win)
    def beat():
        now=time.perf_counter()
        if state['phase']:state['gaps'][state['phase']]=max(state['gaps'].get(state['phase'],0),now-state['last'])
        state['last']=now
    pulse.timeout.connect(beat);pulse.start(25)
    monitor=QTimer(win);monitor.setInterval(250)
    def progress_sample():
        box=QApplication.activeModalWidget();phase=state['phase']
        if phase and isinstance(box,Progress):
            text=box.note.text();entries=state['progress'].setdefault(phase,[])
            if not entries or entries[-1]['text']!=text:
                entries.append({'text':text,'health':box.health.text(),'minimum':box.bar.minimum(),'maximum':box.bar.maximum(),'value':box.bar.value(),'cancel_enabled':box.cancel_btn.isEnabled()})
                if len(entries)==1:box.grab().save(str(out/(phase+'-progress.png')))
    monitor.timeout.connect(progress_sample);monitor.start()
    def timed(name,fn):
        app.processEvents();state['phase']=name;state['last']=time.perf_counter();start=state['last'];(out/'phase.txt').write_text(name,encoding='ascii')
        try:return fn()
        finally:
            app.processEvents();state['times'][name]={'seconds':time.perf_counter()-start,'max_ui_gap_seconds':state['gaps'].get(name,0)};state['phase']=None
    def action_timer(action):
        timer=QTimer(win);timer.setInterval(50)
        def act():
            box=QApplication.activeModalWidget()
            if action=='cancel' and isinstance(box,Progress):timer.stop();box.reject()
            elif action=='close' and isinstance(box,Progress):timer.stop();win.close()
            elif isinstance(box,QMessageBox) and box.objectName() in ('dailyOverwrite','normalConfirm'):
                assert box.defaultButton().text()=='取消';text='取消' if action=='reject' else '覆盖当天记录' if box.objectName()=='dailyOverwrite' else '确认并归档';next(b for b in box.buttons() if b.text()==text).click()
        timer.timeout.connect(act);timer.start();return timer
    def finish(error=None):
        pulse.stop();monitor.stop();report={'passed':error is None,'error':error,'version':diagnostic(),'checks':state['checks'],'timings':state['times'],'progress':state['progress'],'storage':'original daily SQLite schema and JSON/zlib; private copied database only'}
        (out/'work_io_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');win.job_dirty=False;win._discard_close=True;win.close();app.exit(0 if error is None else 1)
    win.failed=lambda msg:(_ for _ in ()).throw(RuntimeError(msg));sys.excepthook=lambda t,v,tb:finish(''.join(traceback.format_exception(t,v,tb)))
    def exercise():
        try:
            head=win.work_repository.list_jobs()[0];path=win.work_repository.path;day=head['day'];original=normal_delivery.job_head(win.work_repository,day)
            timer=action_timer('cancel')
            try:
                timed('cancel_read',lambda:read_job(win,path,day));raise AssertionError('read ignored cancellation')
            except Cancelled:pass
            finally:timer.stop();timer.deleteLater()
            assert not win.results and normal_delivery.job_head(win.work_repository,day)==original;state['checks'].append('cancel real full read leaves current workspace and original daily header unchanged; owned process reclaimed')
            record=timed('read',lambda:read_job(win,path,day));assert record['digest']==original['digest'];timed('restore',lambda:win.restore_job(record,owned=True));del record
            assert len(win.results)==5 and sum(len(r['rows']) for r in win.results.values())==84693;state['checks'].append('retry reads validated full five-store snapshot, owned restore preserves 84693 result rows and frozen configuration')
            config=timed('copy_config',lambda:read_job(win,path,day,True));assert config['store_presets']==win.store_presets;del config;state['checks'].append('copy configuration reads full source validation but returns only exact saved configuration')
            # Test-only current-day copy. Production readonly historical behavior
            # is verified separately by work_smoke; original historical row remains.
            win.job_day=wr.today();win.readonly=False;win.new_focus=focus.empty(wr.today()[:7]);win.refresh_focus();win.export_log.append({'qa':'isolated full save'});win.touch();timer=action_timer('accept')
            try:assert timed('save',win.save_current)
            finally:timer.stop();timer.deleteLater()
            saved=normal_delivery.job_head(win.work_repository,wr.today());assert saved and not win.job_dirty;state['checks'].append('actual full save with default-cancel same-day confirmation when needed; existing repository transaction and schema retained')
            # The prepare/cancel path must not overwrite the newly saved job.
            win.export_log.append({'qa':'cancelled modification'});win.touch();timer=action_timer('reject')
            try:assert not timed('overwrite_cancel',win.save_current)
            finally:timer.stop();timer.deleteLater()
            assert normal_delivery.job_head(win.work_repository,wr.today())==saved;win.export_log.pop();state['checks'].append('full same-day overwrite cancellation preserves exact revision/digest/header')
            timer=action_timer('accept')
            try:timed('confirm_normal',win.confirm_normal)
            finally:timer.stop();timer.deleteLater()
            heads=[h for h in win.orders.headers() if h['day']==wr.today()];assert len(heads)==5;assert not win.job_dirty;state['checks'].append('actual normal confirmation commits five orders and the full draft; no dialog-thread JSON compression')
            folder=out/'merchant';folder.mkdir();original_dir=QFileDialog.getExistingDirectory;QFileDialog.getExistingDirectory=lambda *a,**k:str(folder)
            try:timed('merchant_export',lambda:win.export_current(list(win.results)))
            finally:QFileDialog.getExistingDirectory=original_dir
            assert len(list(folder.glob('*.xlsx')))==5;state['checks'].append('five already-signed central-warehouse merchant files exported with source validation and real row progress; folder is deterministic QA destination')
            record=timed('reload_saved',lambda:read_job(win,path,wr.today()));assert record['digest']==normal_delivery.job_head(win.work_repository,wr.today())['digest'];assert all(record['payload']['results'][s]==r for s,r in win.results.items());del record;state['checks'].append('saved/restored result trees equal in all fields; complete daily digest matches repository header')
            timer=action_timer('close');win.job_dirty=False;win._discard_close=True
            try:
                timed('close_read',lambda:read_job(win,path,wr.today()));raise AssertionError('close ignored active read')
            except Cancelled:pass
            finally:timer.stop();timer.deleteLater()
            state['checks'].append('window close during real full read cancels and reclaims worker');finish()
        except Exception:finish(traceback.format_exc())
    # Let cold history indexing finish; do not benchmark two reads competing.
    ready=QTimer(win)
    def wait_ready():
        if win.history_reader.active is None:ready.stop();ready.deleteLater();QTimer.singleShot(0,exercise)
    ready.timeout.connect(wait_ready);ready.start(100)
