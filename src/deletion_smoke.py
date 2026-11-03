"""Opt-in isolated Qt/spawn deletion acceptance, including legacy C parser."""
import copy,json,os,sqlite3,sys,time,traceback
from pathlib import Path
from contextlib import closing
from PySide6.QtCore import QTimer,Qt
from PySide6.QtWidgets import QApplication,QMessageBox,QCheckBox,QPushButton
from PySide6.QtTest import QTest

def start_smoke(app,win,args):
    fixture,out=[Path(x).resolve() for x in args[:2]];checks=[];beats=[]
    def require(ok,msg):
        if not ok:raise AssertionError(msg)
    def wait(predicate):
        end=time.monotonic()+60
        while not predicate() and time.monotonic()<end:QTest.qWait(20)
        require(predicate(),'background timeout')
    def execute():
        heartbeat=QTimer(win);heartbeat.setInterval(25);heartbeat.timeout.connect(lambda:beats.append(time.monotonic()));heartbeat.start()
        controller=QTimer(win);controller.setInterval(25)
        try:
            require(Path(os.environ.get('REPLENISHMENT_DATA_DIR','')).resolve()==out/'settings','isolated settings required')
            out.mkdir(parents=True,exist_ok=True);win.auto_check.stop();win.jst_generator.stopped=True;win.jst_generator.timer.stop();win.aoxiang_workflows.stopping=True;win.aoxiang_workflows.timer.stop()
            import batch_service as bs,warehouse_catalog as wc,work_repository as wr,normal_orders as no,new_product_focus as focus,data_maintenance as dm,data_maintenance_ui as ui,purchases
            from ui_work_io import Session,request,Progress
            from business_ui import run
            from runtime_support import Cancelled
            catalogs={w:wc.import_catalog(fixture/(w+'.xlsx'),w) for w in wc.WAREHOUSES}
            win.catalogs=catalogs
            loaded=run(win,bs.load,([fixture/'sales.xlsx'],catalogs),'验收：检查两店资料');win.loaded_ok(loaded)
            win.profile=json.loads((fixture/'must.json').read_text('utf-8'));win.new_focus=focus.empty(wr.today()[:7]);codes=list(loaded['stores']);a,b=codes[:2]
            for code in codes:win.set_store_ui(code,{**win.store_ui(code),'mode':1,'primary':'河北仓','secondary':'山东仓'})
            win.start_calculation(codes);wait(lambda:win.worker is None);require(all(win.states[c]['state']=='已完成' for c in codes),str(win.states));original=copy.deepcopy(win.results)
            with Session(win.work_repository.path) as session:
                request(win,session,'prepare_save',win.snapshot());request(win,session,'save',None)
                request(win,session,'prepare_normal',(win.snapshot(),codes));request(win,session,'confirm_normal',None)
            repo=no.Repository(win.work_repository);heads=purchases.targets(win.work_repository.path);chosen=[h for h in heads if h['store']==a];before=repo.get(win.job_day,b)
            # Exercise compressed legacy input through the real frozen worker.
            with closing(sqlite3.connect(win.work_repository.path)) as db,db:
                db.execute('INSERT INTO normal_work_revisions VALUES(?,?,?)',('deletion-smoke-legacy',win.job_day,no.encode({'states':{a:{},b:{}},'probe':list(range(800))})))
            checks.append('two-store Windows spawn calculation, draft save and atomic signature')
            phase={'mode':'preview_cancel','saw':[]}
            def click_dialog():
                d=app.activeModalWidget()
                if not d:return
                if d.windowTitle()=='彻底删除订单及关联数据':
                    d.grab().save(str(out/'delete-preview.png'))
                    if phase['mode']=='preview_cancel':d.reject();phase['saw'].append('preview_cancel');return
                    for box in d.findChildren(QCheckBox):box.setChecked(True)
                    next(x for x in d.findChildren(QPushButton) if x.text()=='永久删除上述订单').click()
                elif isinstance(d,Progress) and d.operation=='delete_orders' and phase['mode']=='close_cancel':
                    phase['saw'].append('close_cancel');phase['mode']='waiting_cancel';d.close()
                elif isinstance(d,QMessageBox):
                    phase['saw'].append(d.windowTitle());d.accept()
            controller.timeout.connect(click_dialog);controller.start()
            ui.remove(win,win,[chosen[0]]);require(repo.get(win.job_day,a) is not None,'preview cancel deleted order')
            checks.append('actual deletion preview cancelled without mutation')
            phase['mode']='close_cancel';ui.remove(win,win,[chosen[0]])
            require('close_cancel' in phase['saw'] and repo.get(win.job_day,a) is not None,'close during preparation did not safely cancel')
            checks.append('close deletion progress: safe cancellation and original order retained')
            phase['mode']='confirm';ui.remove(win,win,[chosen[0]])
            require('已彻底删除' in phase['saw'] and '订单已删除，页面未完成刷新' not in phase['saw'],str(phase))
            require(repo.get(win.job_day,a) is None and repo.get(win.job_day,b)==before,'wrong deletion scope or sibling snapshot changed')
            require(a not in win.results and win.results[b]==original[b],'remaining current calculation changed')
            require(a not in win.store_widgets and b in win.store_widgets,'current table not refreshed')
            checks.append('retry after cancellation; legacy ijson parser in work process; exact sibling signed/current result retained; current table refreshed')
            win.show_section(2);win.followup.query();QTest.qWait(300);win.grab().save(str(out/'after-delete.png'))
            with Session(win.work_repository.path) as session:
                try:request(win,session,'delete_preview',[chosen[0]])
                except RuntimeError:checks.append('repeat removed target rejected; process error returned without blocking')
                else:raise AssertionError('removed target accepted')
            from version_info import diagnostic
            intervals=[y-x for x,y in zip(beats,beats[1:])]
            report={'passed':True,'frozen_exe':bool(getattr(sys,'frozen',False)),'checks':checks,'heartbeat_ticks':len(beats),'max_gap_seconds':max(intervals,default=0),'scope':'whole scripted walk, including synchronous fixture checks/screenshots',**diagnostic()}
        except Exception:report={'passed':False,'frozen_exe':bool(getattr(sys,'frozen',False)),'checks':checks,'error':traceback.format_exc()}
        finally:
            controller.stop();heartbeat.stop()
            (out/'deletion-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
            win._discard_close=True;app.exit(0 if report['passed'] else 1)
    QTimer.singleShot(200,execute)
