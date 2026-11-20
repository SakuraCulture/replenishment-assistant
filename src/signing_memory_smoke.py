"""Explicit isolated native UI/spawn acceptance. No business/platform fixtures."""
import hashlib
import json
import os
import sys
import time
import traceback
from contextlib import closing
from pathlib import Path
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication,QMessageBox,QPushButton
from version_info import diagnostic


def fail_draft_read(connection,path,cancel):
    from draft_spool import Reader
    from work_io_process import serve
    original=Reader.index_tree;opened=0
    def read(reader):
        nonlocal opened
        opened+=1
        if opened==2:
            reader.stream.read(8)
            raise MemoryError('QA_DRAFT_RECEIVE')
        return original(reader)
    Reader.index_tree=read
    serve(connection,path,cancel)


def start_smoke(app,win,args):
    out=Path(args[0]).resolve();phase=args[1];spec=json.loads((out/'synthetic.json').read_text('utf-8'))
    assert spec['synthetic'] is True and spec['live_platform'] is False
    assert win.work_repository.path.resolve()==out/'settings/workbench.sqlite3'
    assert Path(os.environ['REPLENISHMENT_DATA_DIR']).resolve()==out/'settings'
    win.auto_check.stop();win.jst_generator.stopped=True;win.jst_generator.timer.stop()
    win.aoxiang_workflows.timer.stop();win.aoxiang_workflows.stopping=True
    checks=[];dialogs=[];shots=[];state={'seen':set(),'mode':None,'commit':[],'peaks':{},'progress':[]}
    import work_repository as wr,normal_exports,warehouse_execution as execution
    from work_memory import snapshot
    from ui_work_io import Progress,Session
    from resource_pressure_probe import Probe
    probe=Probe(spec.get('resource_profile','ample'))
    input_limit=spec.get('draft_worker_mib')
    session_init=Session.__init__

    def owned_session(session,*args,**kwargs):
        if state['mode']=='input-error':kwargs['worker']=fail_draft_read
        session_init(session,*args,**kwargs)
        try:
            if input_limit:probe.limit(session.process,input_limit,'draft-worker-started')
        except BaseException:
            session.close();raise

    def sign():
        from unittest.mock import patch
        if input_limit or state['mode']=='input-error':
            with patch.object(Session,'__init__',owned_session):win.export_current()
        else:win.export_current()

    def require(ok,text):
        if not ok:raise AssertionError(text)
        checks.append(text)

    def shot(widget,name):
        if widget.grab().save(str(out/name)):shots.append(name)

    def click(dialog,text):
        b=next((b for b in dialog.findChildren(QPushButton) if b.text()==text),None)
        if b is None:raise AssertionError('Missing native button: '+text)
        b.click()

    def sample():
        dialog=QApplication.activeModalWidget()
        try:
            if isinstance(dialog,Progress):
                probe.tick(dialog)
                pid=dialog.session.process.pid
                samples=[]
                for name,process in (('gui',None),('worker',pid)):
                    data=snapshot(process);samples.append(data)
                    for field in ('working_bytes','private_bytes'):
                        key=name+'_'+field;state['peaks'][key]=max(state['peaks'].get(key,0),data.get(field,0))
                for field in ('working_bytes','private_bytes'):
                    if all(field in data for data in samples):
                        key='combined_'+field
                        state['peaks'][key]=max(state['peaks'].get(key,0),sum(data[field] for data in samples))
                text=dialog.note.text()
                if len(state['progress'])<500 and (not state['progress'] or time.monotonic()-state['progress'][-1]['at']>2):
                    state['progress'].append({'at':time.monotonic(),'operation':dialog.operation,'stage':text,'worker_pid':pid,'committing':dialog.committing,'worker_private_bytes':data.get('private_bytes'),'worker_working_bytes':data.get('working_bytes')})
                    (out/'stage.json').write_text(json.dumps(state['progress'][-1],ensure_ascii=False),encoding='utf-8')
                if dialog.committing and not state['commit']:state['commit'].append('observed')
                if state['mode']=='cancel' and dialog.operation=='delivery_execute' and ('编码内容块' in text or '流式编码' in text):
                    shot(dialog,'cancel-during-encoding.png');state['cancelled']=True;dialog.reject()
                return
            if dialog is None:return
            identity=(id(dialog),dialog.windowTitle(),dialog.objectName())
            if identity in state['seen']:return
            if dialog.objectName()=='merchantSelection':
                state['seen'].add(identity);dialog.destination.setText(str(out/'exports'));dialog.select_all(True)
                for store,warehouse,item,valid in dialog.items:
                    if store not in state['codes']:
                        from PySide6.QtCore import Qt
                        item.setCheckState(Qt.CheckState.Unchecked)
                shot(dialog,'selection-'+state['mode']+'.png');dialog.ok.click()
            elif dialog.objectName()=='deliveryConfirm':
                state['seen'].add(identity);shot(dialog,'confirmation-'+state['mode']+'.png');click(dialog,'确认签发并导出')
            elif dialog.windowTitle()=='签发与导出结果':
                state['seen'].add(identity);shot(dialog,'result-'+state['mode']+'.png');click(dialog,'继续本次补货')
            elif isinstance(dialog,QMessageBox):
                state['seen'].add(identity);text=dialog.text();dialogs.append({'title':dialog.windowTitle(),'text':text,'details':dialog.detailedText()})
                shot(dialog,'message-'+state['mode']+'.png')
                if state['mode']=='cancel':require('Cancelled' in text and '未开始最终提交' in text,'真实取消回执明确未提交')
                elif state['mode']=='failure':require('QA_BATCH_ABORT' in text,'原始写入错误在真实弹窗中可见')
                elif state['mode']=='input-error':
                    require('MemoryError' in text and 'QA_DRAFT_RECEIVE' in text and '未开始最终提交' in text,'草稿接收的首个错误直接显示在实际弹窗中')
                else:raise AssertionError('Unexpected modal: '+dialog.windowTitle()+': '+text)
                dialog.accept()
        except Exception:
            state['error']=traceback.format_exc()
            if dialog is not None:dialog.reject()
    timer=QTimer(win);timer.setInterval(100);timer.timeout.connect(sample);timer.start()

    def exercise():
        report={'passed':False,'phase':phase,'frozen_exe':bool(getattr(sys,'frozen',False)),
                'version':diagnostic(),'checks':checks,'dialogs':dialogs,'screenshots':shots,'live_platform':False}
        payload=None
        try:
            require(win.concurrency.value()==2,'默认计算并发仍为2，未降低并发')
            if phase=='reopen':
                expected=json.loads((out/'expected.json').read_text('utf-8'))
                require(len(win.orders.headers())==expected['files'],'实际EXE重新打开后全部签发仍存在')
                with closing(win.work_repository.connect()) as db:
                    require(db.execute('PRAGMA quick_check').fetchone()[0]=='ok','重开订单库完整性通过')
                    require(db.execute('SELECT count(*) FROM warehouse_execution_heads').fetchone()[0]==expected['files'],'独立仓单数量保持')
                    require(db.execute('SELECT digest FROM jobs').fetchone()[0]==expected['work_digest'],'完整草稿摘要保持')
                require(len(list((out/'exports').glob('*.xlsx')))==expected['files'],'全部实际导出文件保留')
            else:
                from signing_memory_fixture import fixture
                files=spec['files'];rows=spec['rows'];(out/'exports').mkdir(exist_ok=True)
                payload,codes=fixture(rows,files,spec.get('extra',1000));state['codes']=codes
                win.results=payload['results'];win.states=payload['states'];win.loaded=payload['loaded'];win.input_dirty=False
                win.job_day=payload['day'];win.readonly=False;win.job_dirty=True;win.new_focus=None
                # Fixture injection replaces only UI data collection. All
                # dialogs, IPC, validation, encoding, DB commit and XLSX writes
                # are the unmodified user action below.
                win.snapshot=lambda:payload
                if phase=='input-error':
                    state.update(mode='input-error',seen=set());started=time.perf_counter();sign()
                    report['input_error_seconds']=time.perf_counter()-started
                    if state.get('error'):raise AssertionError(state['error'])
                    require(len(dialogs)==1 and report['input_error_seconds']<30,'接收故障在正常超时阈值前结束')
                    require(not win.orders.headers() and win.work_repository.get_job(wr.today()) is None,'接收失败保留为空的草稿和仓单')
                    require(not list((out/'settings/.encoding-cache').glob('*.tmp')),'接收失败后临时资料已释放')
                if phase=='boundaries':
                    state.update(mode='cancel',seen=set());win.export_current()
                    require(state.get('cancelled') and not win.orders.headers(),'真实编码进度页取消后整批未提交')
                    require(not list((out/'settings/.encoding-cache').glob('*.tmp')),'取消后私有临时文件已关闭')
                    with closing(win.work_repository.connect()) as db,db:
                        db.execute("CREATE TRIGGER qa_abort BEFORE INSERT ON normal_revisions WHEN (SELECT COUNT(*) FROM normal_revisions)>0 BEGIN SELECT RAISE(ABORT,'QA_BATCH_ABORT'); END")
                    if state.get('error'):raise AssertionError(state['error'])
                    state.update(mode='failure',seen=set());win.export_current()
                    require(not win.orders.headers(),'真实EXE第二单写入失败，第一单和草稿一起回滚')
                    require(win.work_repository.get_job(wr.today()) is None,'失败不留下半批草稿')
                    with closing(win.work_repository.connect()) as db,db:db.execute('DROP TRIGGER qa_abort')
                state.update(mode='success',seen=set());started=time.perf_counter();sign()
                if state.get('error'):raise AssertionError(state['error'])
                report['action_seconds']=time.perf_counter()-started
                require(len(win.orders.headers())==files,'真实签发并导出动作完成全部父单')
                with closing(win.work_repository.connect()) as db:
                    heads=execution.heads(db);work_digest=db.execute('SELECT digest FROM jobs').fetchone()[0]
                    require(len(heads)==files,'全部仓单已归档，无额外订单')
                    require(db.execute('PRAGMA quick_check').fetchone()[0]=='ok','签发后订单库完整性通过')
                import openpyxl
                for head in heads:
                    path=out/'exports'/normal_exports.filename({'day':head['day'],'store':head['store'],'payload':{'result':payload['results'][head['store']]}},head['warehouse'])
                    book=openpyxl.load_workbook(path,read_only=True,data_only=False)
                    try:
                        actual=[(r[4],r[16],r[15]) for name in normal_exports.SHEETS for r in book[name].iter_rows(min_row=2,values_only=True) if r and len(r)>16 and r[4] is not None]
                    finally:book.close()
                    expected=[(r['E'],r['AS'],r['V']) for r in payload['results'][head['store']]['rows']]
                    require(actual==expected,head['store']+'：实际Excel逐行SKU、数量、单价及顺序一致')
                require(not list((out/'settings/.encoding-cache').glob('*.tmp')),'成功后编码临时文件已自动关闭')
                if input_limit:
                    require(any(e['stage']=='draft-worker-started' and e['limit_mib']==input_limit for e in probe.events),'签发子进程从启动到导出均在指定内存限额内')
                if spec.get('resource_profile')=='occupied':
                    require(any(e['stage']=='encoding-pressure' for e in probe.events),'实际EXE编码期间仅对自有子进程收紧内存预算')
                    require(any(e['stage']=='encoding-resources-released' for e in probe.events),'实际EXE编码期间资源释放后继续完成')
                    require(any(e['stage']=='export-pressure' for e in probe.events),'实际EXE导出期间中途收紧资源后文件仍完整')
                elif spec.get('resource_profile')=='tight':
                    require(any(e['stage']=='encoding-tight' for e in probe.events),'实际EXE在紧张进程预算下完整签发并导出')
                (out/'expected.json').write_text(json.dumps({'files':files,'rows':rows,'work_digest':work_digest}),encoding='utf-8')
                report.update(files=files,rows_per_file=rows,peaks=state['peaks'],draft_worker_mib=input_limit)
            report['passed']=True;shot(win,'completed-'+phase+'.png')
        except Exception:report['error']=traceback.format_exc()
        finally:
            timer.stop();report['progress']=state['progress'];report['resource_profile']=spec.get('resource_profile','ample');report['resource_pressure_events']=probe.events;probe.close()
            (out/('report-'+phase+'.json')).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            # Exercise the real asynchronous close. A fixed exit timer can end
            # the Qt loop while a resource-constrained cleanup is still running.
            win.job_dirty=False;win._discard_close=True;win.close()
    QTimer.singleShot(350,exercise)
