"""Opt-in real Qt import/review/save/reopen acceptance on marked fixtures only."""
import hashlib,json,os,shutil,sqlite3,sys,time,traceback,threading
from contextlib import closing
from pathlib import Path
from PySide6.QtCore import Qt,QTimer
from PySide6.QtWidgets import QMessageBox,QPushButton
from PySide6.QtTest import QTest


def start_smoke(app,win,args):
    out=Path(args[0]).resolve();mode=args[1];marker=json.loads((out/'synthetic.json').read_text('utf-8'))
    assert marker['synthetic'] and not marker['live_platform']
    assert Path(os.environ['REPLENISHMENT_DATA_DIR']).resolve()==out/'settings'
    assert win.work_repository.path.resolve()==out/'settings/workbench.sqlite3'
    win.auto_check.stop();win.jst_generator.stopped=True;win.jst_generator.timer.stop()
    win.aoxiang_workflows.stopping=True;win.aoxiang_workflows.timer.stop()
    fixture=json.loads((out/'fixture.json').read_text('utf-8'));heads=fixture['heads'];path=win.work_repository.path
    checks=[];dialogs=[];messages=[];beats=[];expected=[None];timings=[];statuses=[];commit_states=[]
    import work_repository as wr,order_lifecycle as lc
    from lifecycle_batch_ui import BatchImport
    def require(ok,text):
        if not ok:raise AssertionError(text)
        checks.append(text)
    def facts():
        with closing(sqlite3.connect(path)) as db:
            tables=[n for n, in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            return {n:db.execute('SELECT * FROM "'+n+'" ORDER BY rowid').fetchall() for n in tables if n.startswith('lifecycle_') or n=='jst_tasks'}
    def originals():
        h=hashlib.sha256()
        with closing(sqlite3.connect(path)) as db:
            for name in ('normal_orders','normal_revisions','normal_work_revisions','source_files','snapshot_chunks'):
                for row in db.execute('SELECT * FROM "'+name+'" ORDER BY rowid'):
                    for value in row:
                        raw=value if isinstance(value,bytes) else wr.dumps(value).encode('utf-8');h.update(len(raw).to_bytes(8,'big')+raw)
        return h.hexdigest()
    def document_signature():
        with closing(sqlite3.connect(path)) as db:
            rows=db.execute('SELECT * FROM lifecycle_versions ORDER BY oid,revision,warehouse,stage,version').fetchall()
        return hashlib.sha256(wr.dumps(rows).encode()).hexdigest()
    def tick():
        beats.append(time.monotonic());box=app.activeModalWidget()
        task=getattr(win,'_large_io_dialog',None)
        if task is not None and hasattr(task,'worker') and not task.property('observedReturnSaveSmoke'):
            task.setProperty('observedReturnSaveSmoke',True);task.worker.status.connect(statuses.append);task.worker.commit_state.connect(commit_states.append)
        if isinstance(box,QMessageBox):
            detail=box.text()+'\n'+box.informativeText()+'\n'+box.detailedText()
            messages.append({'title':box.windowTitle(),'detail':detail,'expected':expected[0]})
            box.grab().save(str(out/(mode+'-message-'+str(len(messages))+'.png')))
            QTest.mouseClick(box.button(QMessageBox.StandardButton.Ok),Qt.MouseButton.LeftButton) if box.button(QMessageBox.StandardButton.Ok) else box.accept()
    timer=QTimer(win);timer.setInterval(30);timer.timeout.connect(tick)
    def opened(stage,file=None):
        d=BatchImport(win,win,stage,heads);dialogs.append(d);d.show();d.add_paths([str(file or out/'inputs'/(stage+'.xlsx'))])
        require(d.read_ready and len(d.documents)==2,'真实文件读取与整单识别：'+stage)
        d.preview();require(d.review is not None and len(d.plan['plans'])==2,'真实核对页生成两个仓单：'+stage)
        return d
    def save(d,name):
        d.review.complete.setChecked(True);d.review.extra_ack.setChecked(True);d.review.grab().save(str(out/(mode+'-'+name+'.png')))
        t=time.monotonic();QTest.mouseClick(d.review.save,Qt.MouseButton.LeftButton);timings.append({'name':name,'click_seconds':time.monotonic()-t})
    def run():
        timer.start()
        try:
            signed=originals()
            if mode=='exercise':
                before=facts();d=opened('return')
                next(b for b in d.review.findChildren(QPushButton) if b.text()=='返回文件范围').click();d.reject()
                require(facts()==before,'返回与取消不保存任何回传事实')
                for stage in ('return','operations'):
                    d=opened(stage);values=[(p['head'],p['digest'],wr.dumps(p['payload'])) for p in d.plan['plans']];save(d,stage+'-review')
                    require(d.receipt is not None and all(r['changed'] for r in d.receipt),'真实确认保存按钮完成：'+stage)
                    with closing(sqlite3.connect(path)) as db:
                        for h,digest,raw in values:
                            row=db.execute('SELECT digest,payload FROM lifecycle_versions WHERE oid=? AND revision=? AND warehouse=? AND stage=?',(*lc.key(h),stage)).fetchone()
                            require(row==(digest,raw),'写入数量、顺序、完整JSON及摘要与预览一致：'+stage+'/'+h['warehouse'])
                    before=facts();again=opened(stage);save(again,stage+'-same')
                    require(again.receipt is not None and not any(r['changed'] for r in again.receipt) and facts()==before,'相同文件不新增版本、事件或原件：'+stage)
                import openpyxl
                file=out/'inputs/operations.xlsx';book=openpyxl.load_workbook(file)
                book.active.cell(2,book.active.max_column,'读锁验收：只改说明，数量保持');book.save(file);book.close()
                d=opened('operations');ready=threading.Event();release=threading.Event()
                def reader():
                    with closing(sqlite3.connect(path)) as db:
                        db.execute('BEGIN');db.execute('SELECT * FROM normal_orders').fetchall();ready.set();release.wait(5);db.rollback()
                thread=threading.Thread(target=reader);thread.start();require(ready.wait(2),'实际EXE建立受控短暂读锁')
                unlock=threading.Timer(.45,release.set);unlock.start();before_status=len(statuses);before_states=len(commit_states)
                try:save(d,'short-reader')
                finally:release.set();thread.join();unlock.cancel()
                require(d.receipt is not None and all(r['version']==2 for r in d.receipt),'短暂读锁后整批只保存一次新运营版本')
                with closing(sqlite3.connect(path)) as db:
                    require(db.execute("SELECT COUNT(*) FROM lifecycle_versions WHERE stage='operations'").fetchone()[0]==4,'回滚尝试不遗留重复单据版本')
                    for p in d.plan['plans']:
                        row=db.execute("SELECT payload FROM lifecycle_versions WHERE oid=? AND revision=? AND warehouse=? AND stage='operations' AND version=2",lc.key(p['head'])).fetchone()
                        require(row[0]==wr.dumps(p['payload']),'读锁重试仍保持完整JSON、数量和顺序：'+p['head']['warehouse'])
                require(any('已安全回滚' in m for m in statuses[before_status:]),'实际Qt进度显示回滚重核后重试，没有跳过校验')
                require(False in commit_states[before_states:],'安全回滚后恢复可取消状态，重新提交前仍保护原子边界')
                bad=out/'inputs'/'after-preview.xlsx';shutil.copy2(out/'inputs/operations.xlsx',bad)
                d=opened('operations',bad);before=facts();bad.write_bytes(bad.read_bytes()+b'changed-after-preview');expected[0]='原文件'
                save(d,'changed-file');expected[0]=None
                require(d.receipt is None and facts()==before,'原文件预览后变化：真实错误提示且不保存任何部分')
                require(messages and '原文件' in messages[-1]['detail'],'实际错误窗口保留原文件变化原因')
                d.reject();require(originals()==signed,'原签发、源文件和内容块保持不变')
                (out/'expected-documents.json').write_text(json.dumps({'signature':document_signature(),'originals':signed}),'utf-8')
            else:
                expected_values=json.loads((out/'expected-documents.json').read_text('utf-8'))
                require(document_signature()==expected_values['signature'] and originals()==expected_values['originals'],'实际EXE退出重开：全部已保存事实与原签发不变')
                before=facts();d=opened('operations');save(d,'reopen-same')
                require(d.receipt is not None and not any(r['changed'] for r in d.receipt) and facts()==before,'重开后的相同运营文件仍幂等，不依赖旧进程证据')
            require(not any(m['expected'] is None for m in messages),'没有隐藏或忽略非预期错误窗口')
            require(len(beats)>3,'读取、核对与保存期间Qt事件循环持续运行')
            result={'passed':True,'checks':checks,'frozen_exe':bool(getattr(sys,'frozen',False)),'dialogs_not_stubbed':True,'synthetic':True,'live_platform':False,'timings':timings,'messages':messages,'heartbeat_ticks':len(beats),'statuses':statuses,'commit_states':commit_states}
        except Exception as exc:result={'passed':False,'checks':checks,'error':str(exc),'traceback':traceback.format_exc(),'messages':messages,'timings':timings}
        finally:
            timer.stop()
            for d in reversed(dialogs):
                if not d.busy:d.close();d.deleteLater()
        (out/(mode+'-report.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2),'utf-8',newline='\n')
        win._discard_close=True;app.exit(0 if result['passed'] else 1)
    QTimer.singleShot(100,run)
