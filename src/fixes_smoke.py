"""Actual Qt/spawn cancellation and batch overwrite dialog checks on QA inputs."""
import copy,json,pickle,sys,threading,time,traceback,zlib
from pathlib import Path
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication,QMessageBox
from ui_work_io import Session,request,Progress
from runtime_support import Cancelled
import work_repository as wr,normal_delivery as nd,new_product_focus as focus,storage_chunks
from version_info import diagnostic

def start_smoke(app,win,args):
    fixture,real_file,out=map(Path,args[:3]);checks=[];events=[];pulse=QTimer(win);pulse.setInterval(10);state={'mode':None,'commit':False,'acted':False};last=[time.perf_counter()];gaps=[];original_commit=Progress.commit_state
    def commit_state(dialog,active):
        original_commit(dialog,active);events.append({'active':active,'cancel_enabled':dialog.cancel_btn.isEnabled()})
        if not active:state['commit']=True
    Progress.commit_state=commit_state
    def tick():
        now=time.perf_counter();gaps.append(now-last[0]);last[0]=now;box=QApplication.activeModalWidget()
        if isinstance(box,Progress) and not state['acted']:
            if state['mode']=='encoding' and box.operation=='confirm_normal' and '编码内容块' in box.note.text():state['acted']=True;box.grab().save(str(out/'cancel-encoding.png'));box.reject()
            if state['mode']=='lock' and box.operation=='confirm_normal' and '等待归档写锁' in box.note.text():state['acted']=True;box.grab().save(str(out/'cancel-lock.png'));box.reject()
            if state['mode']=='export' and state['commit'] and box.operation=='delivery_execute' and '门店 1/1' in box.note.text() and box.bar.maximum()==1000 and box.bar.value()>0:state['acted']=True;box.grab().save(str(out/'cancel-export.png'));box.reject()
        if isinstance(box,QMessageBox) and box.objectName()=='deliveryConfirm' and state['mode']=='preview':
            text=box.detailedText();state['preview']=text;pairs={}
            for line in text.splitlines():
                parts=line.split(' | ')
                if len(parts)==7 and parts[1]=='商品执行量':pairs[parts[2]]=(float(parts[-2]),float(parts[-1]))
            assert pairs['moving']==(5,0) and pairs['new_must']==(0,6) and pairs['new_anomaly']==(1,0),pairs
            assert box.defaultButton().text()=='取消';box.grab().save(str(out/'batch-preview.png'));state['preview']=text;box.defaultButton().click()
    pulse.timeout.connect(tick);pulse.start()
    def finish(error=None):
        if state.get('finished'):return
        state['finished']=True
        pulse.stop();Progress.commit_state=original_commit;r={'passed':error is None,'error':error,'checks':checks,'commit_button_events':events,'max_ui_gap_seconds':max(gaps,default=0),'event_ticks':len(gaps),'version':diagnostic(),'batch_preview':state.get('preview')};(out/'fixes_ui_report.json').write_text(json.dumps(r,ensure_ascii=False,indent=2),encoding='utf-8');win.job_dirty=False;win._discard_close=True;win.close();app.exit(0 if error is None else 1)
    def exercise():
        try:
            small=wr.loads(fixture.read_text(encoding='utf-8'));small['day']=wr.today();p=copy.deepcopy(small);result=pickle.loads(zlib.decompress(real_file.read_bytes()));s=result['store'];p['results']={s:result};p['states']={s:{'state':'已完成','name':result['store_name']}};p['new_focus']=focus.empty(p['day'][:7]);work=win.work_repository;repo=win.orders
            for mode in ['encoding','lock']:
                state.update(mode=mode,acted=False)
                with Session(work.path) as session:
                    request(win,session,'prepare_normal',[p,[s]])
                    locker=None
                    if mode=='lock':
                        storage_chunks.backup_before_write(work.path);locker=work.connect();locker.execute('BEGIN IMMEDIATE')
                    try:
                        request(win,session,'confirm_normal',None);raise AssertionError('precommit UI cancel accepted')
                    except Cancelled:pass
                    finally:
                        if locker:locker.rollback();locker.close()
                assert state['acted'] and not repo.headers() and not work.list_jobs() and not repo.events();checks.append('actual EXE cancel '+mode+' leaves all business tables empty, worker reaped')
            state.update(mode='export',commit=False,acted=False);folder=out/'cancelled';folder.mkdir()
            with Session(work.path) as session:
                request(win,session,'delivery_prepare',[p,[s]])
                try:request(win,session,'delivery_execute',str(folder));raise AssertionError('postcommit export did not cancel')
                except RuntimeError as exc:assert '已归档' in str(exc) and '未生成' in str(exc)
            assert state['acted'] and repo.head(p['day'],s)['revision']==1 and not list(folder.glob('*.xlsx'));assert any(e['active'] and not e['cancel_enabled'] for e in events);checks.append('actual COMMIT signals disable cancellation; row-write cancellation retains V001 and reports remaining file')
            state['mode']=None;retry=out/'retry';retry.mkdir()
            with Session(work.path) as session:
                request(win,session,'delivery_prepare',[p,[s]]);r=request(win,session,'delivery_execute',str(retry))
            assert not r['signed'] and len(r['paths'])==1 and repo.head(p['day'],s)['revision']==1;checks.append('actual packaged retry clears old cancellation and exports exact existing V001 without a new revision')
            before=copy.deepcopy(small);before['results']['QA_A']=copy.deepcopy(small['results']['QA_B']);r=before['results']['QA_A'];r.update(store='QA_A',store_name=small['results']['QA_A']['store_name'])
            for d in r['rows']:d.update(B='QA_A',A=r['store_name'])
            repo.confirm(before,['QA_A'],nd.job_head(work,p['day'])['revision'],{'QA_A':None});snapshot=win.snapshot;win.snapshot=lambda:small;win.input_dirty=False;state['mode']='preview'
            assert win.worker is None and not win.input_dirty
            assert repo.get(p['day'],'QA_A')['payload']['result']['summary']['amount']==small['results']['QA_A']['summary']['amount']==12
            errors=[];win.failed=errors.append
            try:win.export_current(['QA_A'])
            finally:win.snapshot=snapshot
            assert not errors,errors
            assert state.get('preview') and repo.head(p['day'],'QA_A')['revision']==1,win.status.text();checks.append('actual batch export confirmation shows same-total 12 yuan SKU changes 5→0 / 0→6 / 1→0, default cancel leaves V001')
            finish()
        except Exception:finish(traceback.format_exc())
    sys.excepthook=lambda t,v,tb:finish(''.join(traceback.format_exception(t,v,tb)))
    QTimer.singleShot(500,exercise)
