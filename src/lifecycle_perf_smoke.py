"""Actual Qt workflow on opt-in synthetic settings; no dialog stubs or platforms."""
import hashlib,json,os,shutil,sqlite3,sys,threading,time,traceback
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
    win.auto_check.stop();win.jst_generator.stopped=True;win.jst_generator.timer.stop();win.aoxiang_workflows.stopping=True;win.aoxiang_workflows.timer.stop()
    import work_repository as wr,order_lifecycle as lc,jst_orders as jo,jst_batches as jb
    from lifecycle_batch_ui import BatchImport
    from jst_ui import GenerationDialog
    heads=json.loads((out/'fixture.json').read_text('utf-8'))['heads'];path=win.work_repository.path
    checks=[];dialogs=[];messages=[];ticks=[];statuses=[];states=[];timings=[];expected=[None];reader_release=[None]
    def require(ok,message):
        if not ok:raise AssertionError(message)
        checks.append(message)
    def signature(tables):
        h=hashlib.sha256()
        with closing(sqlite3.connect(path)) as db:
            for table in tables:
                for row in db.execute('SELECT * FROM "'+table+'" ORDER BY rowid'):
                    values=[{'blob':v.hex()} if isinstance(v,bytes) else v for v in row];h.update(wr.dumps([table,values]).encode());h.update(b'\n')
        return h.hexdigest()
    originals=lambda:signature(('normal_orders','normal_revisions','normal_work_revisions','source_files','snapshot_chunks'))
    def facts():
        with closing(sqlite3.connect(path)) as db:tables=[n for n, in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name") if n.startswith(('lifecycle_','jst_'))]
        return signature(tables)
    def report_status(message):
        statuses.append(message)
        if reader_release[0] is not None and '锁单尝试已安全回滚' in message:reader_release[0].set()
    def tick():
        ticks.append(time.monotonic());task=getattr(win,'_large_io_dialog',None)
        if task is not None and hasattr(task,'worker') and task.worker and not task.property('observedLifecyclePerfSmoke'):
            task.setProperty('observedLifecyclePerfSmoke',True);task.worker.status.connect(report_status);task.worker.commit_state.connect(states.append)
        box=app.activeModalWidget()
        if isinstance(box,QMessageBox):
            text=box.text()+'\n'+box.informativeText()+'\n'+box.detailedText();title=box.windowTitle()
            confirmation=title in ('确认生成聚水潭订单','确认已在聚水潭下单')
            messages.append({'title':title,'detail':text,'confirmation':confirmation,'expected':expected[0]})
            box.grab().save(str(out/(mode+'-message-'+str(len(messages))+'.png')))
            b=box.button(QMessageBox.StandardButton.Yes) if confirmation else box.button(QMessageBox.StandardButton.Ok)
            if b:QTest.mouseClick(b,Qt.MouseButton.LeftButton)
            else:box.reject()
    timer=QTimer(win);timer.setInterval(20);timer.timeout.connect(tick)
    def wait(fn):
        deadline=time.monotonic()+45
        while not fn() and time.monotonic()<deadline:QTest.qWait(20)
        if not fn():raise AssertionError('等待实际界面任务超时')
    def opened(stage,file=None):
        d=BatchImport(win,win,stage,heads);dialogs.append(d);d.show();d.add_paths([str(file or out/'inputs'/(stage+'.xlsx'))])
        require(d.read_ready and len(d.documents)==2,'实际文件识别两个仓单：'+stage)
        d.preview();require(d.review is not None and len(d.plan['plans'])==2,'实际核对页及原有校验：'+stage)
        return d
    def save(d,name):
        d.review.complete.setChecked(True);d.review.extra_ack.setChecked(True);d.review.grab().save(str(out/(mode+'-'+name+'.png')))
        started=time.monotonic();QTest.mouseClick(d.review.save,Qt.MouseButton.LeftButton);timings.append({'name':name,'click_seconds':time.monotonic()-started})
    def generate_and_lock():
        d=GenerationDialog(win,win,list(reversed(heads)));dialogs.append(d);d.show();wait(lambda:bool(d.catalogs))
        QTest.mouseClick(d.preview_btn,Qt.MouseButton.LeftButton)
        require(d.plan is not None and len(d.plan['payload']['plans'])==2,'实际分配预览两个独立仓单')
        frozen=[{'head':p['head'],'rows':p['rows'],'summary':p['summary']} for p in d.plan['payload']['plans']]
        expected_order=[p['head']['number'] for p in frozen]
        d.accept_issues.setChecked(True);QTest.mouseClick(d.generate_btn,Qt.MouseButton.LeftButton);wait(lambda:d.worker is None and d.receipt is not None)
        require(not d.receipt['errors'] and len(d.saved)==2,'实际确认生成保存两张分配单')
        generated=[jo.get(path,g) for g in d.saved]
        require([p['head']['number'] for p in generated]==expected_order,'分配逐单提交顺序与预览一致')
        require(all(p['rows']==q['rows'] and p['summary']==q['summary'] for p,q in zip(generated,frozen)),'分配数量、金额、SKU顺序和完整结果与预览一致')
        d.grab().save(str(out/'exercise-allocation-saved.png'));before=signature(('jst_generations','jst_reservations','jst_steps','jst_runs'))
        d.completed.selectAll();require(len(d.gids())==2 and d.lock_saved_btn.isEnabled(),'实际选择两张已生成单并启用锁单入口')
        ready=threading.Event();release=threading.Event();errors=[]
        def reader():
            try:
                with closing(sqlite3.connect(path)) as db:
                    db.execute('BEGIN');db.execute('SELECT * FROM normal_orders').fetchall();ready.set();release.wait(5);db.rollback()
            except Exception as exc:errors.append(str(exc));ready.set()
        thread=threading.Thread(target=reader);thread.start();require(ready.wait(2) and not errors,'实际EXE建立受控短暂读锁')
        reader_release[0]=release;start=len(statuses);start_states=len(states)
        try:QTest.mouseClick(d.lock_saved_btn,Qt.MouseButton.LeftButton)
        finally:release.set();thread.join();reader_release[0]=None
        require(not errors and any('锁单尝试已安全回滚' in s for s in statuses[start:]),'实际锁单遇读锁后安全回滚并重新核验')
        require(False in states[start_states:],'实际锁单安全回滚后恢复可取消状态')
        with closing(sqlite3.connect(path)) as db:
            require(db.execute('SELECT COUNT(*) FROM jst_locks WHERE locked=1').fetchone()[0]==2,'两张仓单均成功锁定')
            require(db.execute("SELECT COUNT(*) FROM jst_events WHERE action='确认下单并锁定'").fetchone()[0]==2,'锁单重试不重复新增成功事件')
        require(before==signature(('jst_generations','jst_reservations','jst_steps','jst_runs')),'锁单不改分配数量、金额、顺序或重复占库存')
        before=facts();QTest.mouseClick(d.lock_saved_btn,Qt.MouseButton.LeftButton)
        require(before==facts(),'再次锁定不重复写锁或事件');d.close()
    def execute():
        timer.start();unsafe=False
        try:
            signed=originals()
            if mode=='exercise':
                before=facts();d=opened('return');next(b for b in d.review.findChildren(QPushButton) if b.text()=='返回文件范围').click();d.reject()
                require(facts()==before,'取消核对不保存任何部分')
                for stage in ('return','operations','payment','receipt'):
                    if stage=='payment':generate_and_lock()
                    d=opened(stage);values=[(p['head'],p['digest'],wr.dumps(p['payload'])) for p in d.plan['plans']];save(d,stage+'-save')
                    require(d.receipt is not None and all(r['changed'] for r in d.receipt),'实际确认保存完成：'+stage)
                    with closing(sqlite3.connect(path)) as db:
                        for head,digest,raw in values:
                            row=db.execute('SELECT digest,payload FROM lifecycle_versions WHERE oid=? AND revision=? AND warehouse=? AND stage=?',(*lc.key(head),stage)).fetchone()
                            require(row==(digest,raw),'数量、金额、顺序及完整JSON与预览一致：'+stage+'/'+head['warehouse'])
                    before=facts();d=opened(stage);save(d,stage+'-same')
                    require(d.receipt is not None and not any(r['changed'] for r in d.receipt) and facts()==before,'同原件重复不新增版本、事件、原件或任务：'+stage)
                bad=out/'inputs/after-preview.xlsx';shutil.copyfile(out/'inputs/receipt.xlsx',bad);d=opened('receipt',bad);before=facts()
                bad.write_bytes(bad.read_bytes()+b'changed-after-preview');expected[0]='原文件';save(d,'changed-file');expected[0]=None
                require(d.receipt is None and facts()==before,'原文件预览后变化仍拒绝保存且整批不写入')
                require(messages and '原文件' in messages[-1]['detail'],'实际错误窗口保留原件变化原因');d.reject()
                require(originals()==signed,'原签发、源文件与内容块保持不变')
                (out/'expected-facts.json').write_text(json.dumps({'facts':facts(),'originals':signed}),'utf-8',newline='\n')
            else:
                frozen=json.loads((out/'expected-facts.json').read_text('utf-8'))
                require(facts()==frozen['facts'] and originals()==frozen['originals'],'退出重开后所有已保存事实、分配、锁单及原签发一致')
                for stage in ('payment','receipt'):
                    before=facts();d=opened(stage);save(d,'reopen-'+stage)
                    require(d.receipt is not None and not any(r['changed'] for r in d.receipt) and facts()==before,'重开后重复'+stage+'仍不新增，不依赖旧进程证明')
            require(not any(not m['confirmation'] and m['expected'] is None for m in messages),'没有忽略或隐藏非预期错误窗口')
            require(len(ticks)>3,'实际读取、核对和提交期间Qt事件循环持续运行')
            result={'passed':True,'checks':checks,'frozen_exe':bool(getattr(sys,'frozen',False)),'dialogs_not_stubbed':True,'synthetic':True,'live_platform':False,
                    'heartbeat_ticks':len(ticks),'statuses':statuses,'commit_states':states,'messages':messages,'timings':timings}
        except Exception as exc:result={'passed':False,'checks':checks,'error':str(exc),'traceback':traceback.format_exc(),'messages':messages,'statuses':statuses,'timings':timings}
        finally:
            if reader_release[0] is not None:reader_release[0].set()
            for d in reversed(dialogs):
                if getattr(d,'worker',None) or getattr(d,'export_worker',None):
                    d.reject()
                    try:wait(lambda:not getattr(d,'worker',None) and not getattr(d,'export_worker',None))
                    except AssertionError:unsafe=True
                if not unsafe and not getattr(d,'busy',False):d.close();d.deleteLater()
            timer.stop()
        if unsafe:result.update(passed=False,error='Owned UI task still running; process retained without forced exit')
        (out/(mode+'-report.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2),'utf-8',newline='\n')
        if not unsafe:win._discard_close=True;app.exit(0 if result['passed'] else 1)
    QTimer.singleShot(100,execute)
