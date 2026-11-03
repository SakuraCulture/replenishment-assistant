"""Opt-in isolated actual Qt/EXE and Windows-spawn policy acceptance."""
import copy,json,os,sys,time,traceback
from pathlib import Path
from PySide6.QtCore import QTimer,QEventLoop,Qt
from PySide6.QtWidgets import QApplication,QDialog
from openpyxl import load_workbook
import batch_service,warehouse_catalog,work_repository as wr,new_product_focus,new_products
import normal_orders,normal_exports,restock_policy as policy,restock_policy_ui as ui
from version_info import diagnostic

def start_smoke(app,win,args):
    fixture,out=[Path(p).resolve() for p in args[:2]];checks=[];screens=[]
    def require(condition,message):
        if not condition:raise AssertionError(message)
        checks.append(message)
    def wait(predicate,seconds=60):
        end=time.monotonic()+seconds
        while not predicate() and time.monotonic()<end:
            loop=QEventLoop();QTimer.singleShot(15,loop.quit);loop.exec()
        if not predicate():raise AssertionError('Timed out awaiting application task')
    def shot(widget,name):
        app.processEvents();require(widget.grab().save(str(out/(name+'.png'))),'Screenshot '+name);screens.append(name+'.png')
    def switches(codes,changes,post,accept=True,persist=False):
        seen=[];errors=[];timer=QTimer();timer.setInterval(60)
        def drive():
            try:
                d=QApplication.activeModalWidget()
                if d is None or id(d) in seen:return
                if isinstance(d,ui.SwitchDialog):
                    seen.append(id(d))
                    for k,v in changes.items():d.fields[k].setCurrentIndex(1 if v else 2)
                    if persist:d.scope.setCurrentIndex(d.scope.findData('both'))
                    shot(d,'01_switch_editor');d.accept()
                elif isinstance(d,ui.Review):
                    seen.append(id(d));shot(d,'02_switch_preview');d.risk_check.setChecked(True)
                    d.accept() if accept else d.reject()
                elif isinstance(d,ui.imports.Preview):
                    seen.append(id(d));shot(d,'03_default_preview');d.accept() if accept else d.reject()
            except Exception:errors.append(traceback.format_exc());timer.stop()
        timer.timeout.connect(drive);timer.start()
        try:ui.open_policy(win,win,codes,post=post)
        finally:timer.stop()
        if errors:raise AssertionError(errors)
        require(len(seen)==2,'Actual switch editor and confirmation displayed')
    def execute():
        result={};ticks=[];beat=QTimer();beat.setInterval(25);beat.timeout.connect(lambda:ticks.append(time.monotonic()));beat.start()
        try:
            require(Path(os.environ.get('REPLENISHMENT_DATA_DIR','')).resolve()==out/'settings','Isolated settings directory')
            win.jst_generator.timer.stop();win.jst_generator.stopped=True;win.aoxiang_workflows.timer.stop()
            file=out/'synthetic_policy_sales.xlsx';wb=load_workbook(fixture/'sales.xlsx');ws=wb.active;headers={c.value:c.column for c in ws[1]}
            for cells in ws.iter_rows(min_row=2):
                sku=cells[headers['商品编码']-1].value
                if sku in ('both','small'):
                    for h in ('日销','周销','月销','可用库存','在途库存'):ws.cell(cells[0].row,headers[h],0)
                    ws.cell(cells[0].row,headers['缺货天数（天）'],45 if sku=='both' else 0)
            wb.save(file);wb.close()
            catalogs={w:warehouse_catalog.import_catalog(fixture/(w+'.xlsx'),w) for w in warehouse_catalog.WAREHOUSES}
            data=batch_service.load([file],catalogs);win.loaded_ok(data);win.profile=json.loads((fixture/'must.json').read_text('utf-8'));win.new_focus=new_product_focus.empty(wr.today()[:7])
            codes=list(win.store_widgets);a,b=codes[:2]
            for s in codes:win.set_store_ui(s,{**win.store_ui(s),'mode':1,'primary':'河北仓','secondary':'山东仓'})
            win.start_calculation(codes);wait(lambda:win.worker is None)
            require(all(win.states[s]['state']=='已完成' for s in codes),'Actual Windows spawn computed all stores')
            base=win.results[a];by={d['E']:d for d in base['rows']};require(by['both']['AS']==by['both']['BD'],'Enabled extreme restoration follows carton step')
            require(new_products.is_new(by['small']),'Synthetic new item recognized through Excel import')
            selected=new_products.apply(base,{'small':by['small']['BD']});win.new_bases[a]=base;win.results[a]=selected;win.current_store=a;win.result=selected;win.dirty=False;win.set_rows(selected['rows'],True);win.refresh_tables();win.show_section(1);win.navigate(4)
            repo=normal_orders.Repository(win.work_repository);repo.confirm(win.snapshot(),codes,None,{s:None for s in codes},headers_only=True)
            old_order=repo.get(win.job_day,a);before=copy.deepcopy(win.results);cfg=copy.deepcopy(win.config_for(a))
            switches([a],{'extreme_restore':False,'new_stocking':False},True,accept=False)
            require(win.results==before and win.config_for(a)==cfg,'Preview cancel leaves results and parameters unchanged')
            switches([a],{'extreme_restore':False,'new_stocking':False},True,persist=True)
            require(win.results[b]==before[b],'Unselected store exactly unchanged')
            rows={d['E']:d for d in win.results[a]['rows']}
            from result_semantics import order_quantity
            require(rows['both']['AS']==0 and order_quantity(rows['small'])==0,'Confirmed OFF cancels extreme and selected new quantities')
            require(policy.values(win.work_repository.stores()[a]['preset']['config'])==dict(extreme_restore=False,new_stocking=False),'Both switches saved as store defaults')
            from preissue_ui import FinalModel
            fm=FinalModel({a:win.results[a]}, {},lambda *_:None);idx=next(i for i,(s,d) in enumerate(fm.entries) if d['E']=='small')
            require(not fm.setData(fm.index(idx,9),rows['small']['BD'],Qt.ItemDataRole.EditRole),'Actual quantity editor blocks new quantity while disabled')
            require('已关闭' in fm.invalid[(a,'small')],'Inline guard explains how to enable stocking')
            require(repo.get(win.job_day,a)==old_order,'Previously signed archive untouched')
            target=out/'historical_signed.xlsx';normal_exports.export(old_order,target)
            require(target.is_file() and repo.get(win.job_day,a)==old_order,'Historical merchant export still works without rewriting archive')
            payload=win.snapshot();previous=win.work_repository.get_job(win.job_day)
            win.work_repository.save_job(win.job_day,payload,previous['revision'] if previous else None)
            record=win.work_repository.get_job(win.job_day);win.restore_job(record)
            require(policy.values(win.config_for(a))==dict(extreme_restore=False,new_stocking=False),'Daily save and restore preserve both switches')
            win.loaded_ok(copy.deepcopy(data))
            require(policy.values(win.config_for(a))==dict(extreme_restore=False,new_stocking=False),'Rechecking imported data retains selected store defaults')
            switches([a],{'new_stocking':True},False,persist=True)
            require(win.config_for(a)['restock_switches']==dict(extreme_restore=False,new_stocking=True),'Pre-calculation editor patches only selected switch')
            win.start_calculation([a]);wait(lambda:win.worker is None)
            require(win.results[a]['rows'][0]['AS']==0,'Saved extreme OFF reaches spawned recalculation')
            require(not win.results[a].get('new_products'),'Enabling new stocking does not auto-select products')
            win.current_store=a;win.result=win.results[a];win.dirty=False;win.set_rows(win.result['rows'],True);win.show_section(1);win.navigate(4);shot(win,'04_result_switch_status')
            require(len(ticks)>5,'Qt heartbeat continues during background preparation')
            result={'passed':True,'checks':checks,'frozen_exe':bool(getattr(sys,'frozen',False)),'heartbeat_ticks':len(ticks),'screenshots':screens,**diagnostic()}
        except Exception:result={'passed':False,'checks':checks,'error':traceback.format_exc(),**diagnostic()}
        finally:
            beat.stop()
            from history_tasks import HistoryReader
            for reader in win.findChildren(HistoryReader):reader.shutdown()
            win.aoxiang_workflows.shutdown();win.stop_filtered_summary()
            (out/'switch-report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');app.exit(0 if result.get('passed') else 1)
    QTimer.singleShot(200,execute)
