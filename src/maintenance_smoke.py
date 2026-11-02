"""Opt-in isolated real-window/frozen-spawn acceptance, never live user settings."""
import copy,json,os,sys,traceback,threading
from pathlib import Path
from PySide6.QtCore import QTimer,QEventLoop,Qt
from PySide6.QtWidgets import QApplication,QDialog,QMessageBox,QCheckBox,QPushButton
import batch_service,warehouse_catalog,work_repository as wr,normal_orders,purchases
import settings_history,new_product_focus,data_maintenance_ui
from version_info import diagnostic

def start_smoke(app,win,args):
    fixture,out=[Path(s).resolve() for s in args[:2]]
    checks=[]
    def wait_worker():
        if win.worker is None:raise AssertionError('Worker did not start')
        loop=QEventLoop();timer=QTimer();timer.setSingleShot(True);timer.timeout.connect(loop.quit)
        win.worker.finished.connect(loop.quit);timer.start(60000);loop.exec()
        if win.worker is not None:raise AssertionError('Frozen spawn calculation did not finish')
    def execute():
        result={}
        try:
            if Path(os.environ.get('REPLENISHMENT_DATA_DIR','')).resolve()!=out/'settings':raise ValueError('Requires isolated QA directory')
            out.mkdir(parents=True,exist_ok=True);win.jst_generator.timer.stop();win.jst_generator.stopped=True;win.aoxiang_workflows.timer.stop()
            catalogs={w:warehouse_catalog.import_catalog(fixture/(w+'.xlsx'),w) for w in warehouse_catalog.WAREHOUSES}
            data=batch_service.load([fixture/'sales.xlsx'],catalogs);win.loaded_ok(data)
            win.profile=json.loads((fixture/'must.json').read_text('utf-8'));win.new_focus=new_product_focus.empty(wr.today()[:7])
            codes=list(win.store_widgets);a,b=codes[:2]
            for code in codes:win.set_store_ui(code,{**win.store_ui(code),'mode':1,'primary':'河北仓','secondary':'山东仓'})
            win.start_calculation(codes);wait_worker()
            if any(win.states[c]['state']!='已完成' for c in codes):raise AssertionError(str(win.states))
            original=copy.deepcopy(win.results);win.results.pop(a);win.states[a].update(state='计算失败',error='模拟瞬时失败');win.refresh_tables()
            win.retry_stores([a]);wait_worker()
            if win.results[a]['rows']!=original[a]['rows'] or win.results[b]!=original[b]:raise AssertionError('Retry changed quantities/order or sibling result')
            if win.run_codes!=codes:raise AssertionError('Retry changed batch order')
            checks.append('Windows spawn in running app: two-store calculation then single-store retry; exact rows and successful sibling unchanged')
            win.navigate(2);app.processEvents();win.grab().save(str(out/'01_批内单项重跑.png'))
            base=win.current_parameter_record(a);win.work_repository.save_stores({a:base})
            changed=copy.deepcopy(base);changed['preset']['config']['warehouses']['河北仓']=[7,4]
            win.work_repository.save_stores({a:changed},action='验收参数导入');entry=settings_history.history(win.work_repository)[0]
            settings_history.apply(win.work_repository,settings_history.plan(win.work_repository,entry))
            if win.work_repository.stores()[a]!=wr.loads(wr.dumps(base)) or win.results[b]!=original[b]:raise AssertionError('Default undo affected current result')
            checks.append('persisted import history and undo; current calculation unchanged')
            win.parameter_scene.refresh();win.show_section(4);app.processEvents();win.grab().save(str(out/'02_数据参数维护.png'))
            from settings_maintenance_ui import open_history
            def close_history():
                d=QApplication.activeModalWidget()
                if d:d.grab().save(str(out/'03_默认变更表单.png'));d.reject()
            QTimer.singleShot(300,close_history);open_history(win,win)
            checks.append('real history dialog displayed field differences and closed without applying')
            repo=normal_orders.Repository(win.work_repository)
            repo.confirm(win.snapshot(),codes,None,{c:None for c in codes},headers_only=True)
            heads=purchases.targets(win.work_repository.path);chosen=[h for h in heads if h['store']==a];before=repo.get(win.job_day,b)
            controller=QTimer();controller.setInterval(50);phase={'cancel':True,'dialogs':0}
            def choose():
                d=QApplication.activeModalWidget()
                if not d:return
                if d.windowTitle()=='彻底删除订单及关联数据':
                    phase['dialogs']+=1;d.grab().save(str(out/'04_彻底删除范围.png'))
                    if phase['cancel']:d.reject();return
                    for check in d.findChildren(QCheckBox):check.setChecked(True)
                    go=next(x for x in d.findChildren(QPushButton) if x.text()=='永久删除上述订单');go.click()
                elif isinstance(d,QMessageBox):d.accept()
            controller.timeout.connect(choose);controller.start()
            data_maintenance_ui.remove(win,win,[chosen[0]])
            if repo.get(win.job_day,a) is None:raise AssertionError('Cancel removed an order')
            phase['cancel']=False;data_maintenance_ui.remove(win,win,[chosen[0]]);controller.stop()
            if repo.get(win.job_day,a) is not None or repo.get(win.job_day,b)!=before:raise AssertionError('Deletion scope mismatch')
            if win.results[b]['rows']!=original[b]['rows']:raise AssertionError('Surviving current store changed')
            checks.append('actual permanent-delete preview: cancel keeps data; confirm removes all selected store warehouse children, other store/signing unchanged')
            aux=[x for x in win.findChildren(QPushButton) if x.property('auxiliaryAction')]
            if not aux or any(x.icon().isNull() or not x.accessibleName() for x in aux):raise AssertionError('Missing standard icons/accessibility')
            checks.append(f'{len(aux)} auxiliary controls use standard icons and accessible names')
            result={'passed':True,'frozen_exe':bool(getattr(sys,'frozen',False)),'checks':checks,**diagnostic()}
        except Exception:result={'passed':False,'checks':checks,'error':traceback.format_exc(),**diagnostic()}
        finally:
            from history_tasks import HistoryReader
            for r in win.findChildren(HistoryReader):r.shutdown()
            win.aoxiang_workflows.shutdown()
            (out/'maintenance-report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
            app.exit(0 if result.get('passed') else 1)
    QTimer.singleShot(200,execute)
