"""Opt-in isolated acceptance of real signing UI and scheduled lock contention."""
import copy,json,logging,os,sqlite3,sys,time,traceback
from pathlib import Path
from contextlib import closing
from types import SimpleNamespace
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QMessageBox,QDialogButtonBox
from PySide6.QtTest import QTest

def start_smoke(app,win,args):
    fixture,out=[Path(x).resolve() for x in args[:2]];checks=[];errors=[];beats=[]
    def require(ok,message):
        if not ok:raise AssertionError(message)
    def wait(predicate):
        until=time.monotonic()+60
        while not predicate() and time.monotonic()<until:QTest.qWait(20)
        require(predicate(),'background timeout')
    def execute():
        heartbeat=QTimer(win);heartbeat.setInterval(25);heartbeat.timeout.connect(lambda:beats.append(time.monotonic()));heartbeat.start()
        controller=QTimer(win);controller.setInterval(25);old_hook=sys.excepthook
        import export_locations
        old_save,old_directory=export_locations.save_file,export_locations.directory
        sys.excepthook=lambda t,v,tb:errors.append(''.join(traceback.format_exception(t,v,tb)))
        try:
            require(Path(os.environ.get('REPLENISHMENT_DATA_DIR','')).resolve()==out/'settings','isolated settings required')
            out.mkdir(parents=True,exist_ok=True)
            win.auto_check.stop();win.jst_generator.stopped=True;win.jst_generator.timer.stop()
            win.aoxiang_workflows.stopping=True;win.aoxiang_workflows.timer.stop();win.scheduled.timer.stop()
            wait(lambda:win.scheduled.reader is None)
            import batch_service as bs,warehouse_catalog as wc,work_repository as wr,normal_orders as no,new_product_focus as focus
            from business_ui import run
            win.catalogs={w:wc.import_catalog(fixture/(w+'.xlsx'),w) for w in wc.WAREHOUSES}
            loaded=run(win,bs.load,([fixture/'sales.xlsx'],win.catalogs),'验收：检查两店资料');win.loaded_ok(loaded)
            win.profile=json.loads((fixture/'must.json').read_text('utf-8'));win.new_focus=focus.empty(wr.today()[:7]);codes=list(loaded['stores'])
            for code in codes:win.set_store_ui(code,{**win.store_ui(code),'mode':1,'primary':'河北仓','secondary':'山东仓'})
            win.start_calculation(codes);wait(lambda:win.worker is None)
            require(all(win.states[c]['state']=='已完成' for c in codes),str(win.states));original=copy.deepcopy(win.results)
            repo=no.Repository(win.work_repository)
            export_folder=out/'exports';export_folder.mkdir();diff_path=out/'完整差异.xlsx'
            export_locations.save_file=lambda *a,**k:(str(diff_path),'Excel (*.xlsx)')
            export_locations.directory=lambda *a,**k:str(export_folder)
            phase={'mode':'cancel','seen':[],'results':0};visible={}
            def control():
                d=app.activeModalWidget()
                if not d:return
                title=d.windowTitle()
                if d.objectName()=='deliveryConfirm':
                    now=time.monotonic();key=(id(d),phase['mode'])
                    if key not in visible:visible[key]=now;return
                    if now-visible[key]<.10:return
                    layout=d.layout();table=d.business_table;buttons=d.findChild(QDialogButtonBox)
                    require(layout.indexOf(table)>=0 and table.isVisible(),'difference table detached')
                    require(layout.getItemPosition(layout.indexOf(buttons))[0]>layout.getItemPosition(layout.indexOf(table))[0],'confirmation buttons above/overlapping table')
                    require(d.defaultButton().text()=='取消' and d.escapeButton().text()=='取消','default cancellation changed')
                    mode=phase['mode'];d.grab().save(str(out/(mode+'-confirmation.png')));phase['seen'].append(mode)
                    text={'cancel':'取消','diff':'导出完整差异Excel','confirm':'确认签发并导出'}[mode]
                    if mode=='diff':phase['mode']='confirm'
                    next(b for b in d.buttons() if b.text()==text).click()
                elif title=='签发与导出结果':
                    phase['results']+=1;d.grab().save(str(out/'export-result.png'));d.accept()
                elif isinstance(d,QMessageBox):
                    errors.append('unexpected dialog: '+title+' '+d.text());d.reject()
            controller.timeout.connect(control);controller.start()
            win.export_current(codes)
            require(phase['seen']==['cancel'] and all(repo.get(win.job_day,c) is None for c in codes),'cancel mutated signature')
            checks.append('actual export_current confirmation: table attached, default/escape cancel, cancel leaves signatures absent')
            phase['mode']='diff';win.export_current(codes)
            require(phase['seen']==['cancel','diff','confirm'] and phase['results']==1,str(phase))
            require(diff_path.exists() and list(export_folder.glob('*.xlsx')),'difference/merchant files missing')
            require(all(repo.get(win.job_day,c)['revision']==1 for c in codes),'signed revision missing')
            require(win.results==original,'calculation changed during signing')
            checks.append('actual export_current: export full differences, reopen same confirmation, confirm, archive V001 and export files; calculations unchanged')
            controller.stop()
            # Real timer manager with no schedule, against exclusive isolated SQLite lock.
            m=win.scheduled;m.repo.ensure();wait(lambda:m.reader is None)
            with closing(sqlite3.connect(win.work_repository.path)) as db:
                db.execute('BEGIN EXCLUSIVE')
                old_dialog=getattr(win,'_large_io_dialog',None)
                try:
                    win._large_io_dialog=SimpleNamespace(_completed=False);m.poll();require(m.reader is None,'poll started during maintenance')
                    win._large_io_dialog=old_dialog
                    before=len(beats);started=time.monotonic();m.poll();require(time.monotonic()-started<.3,'poll blocks GUI')
                    wait(lambda:m.reader is None);require(len(beats)-before>10,'GUI heartbeat stopped')
                    require('等待数据库释放' in m.status.text(),m.status.text())
                finally:win._large_io_dialog=old_dialog;db.rollback()
            m.poll();wait(lambda:m.reader is None);require('未启用安排' in m.status.text(),m.status.text())
            checks.append('real scheduled manager: maintenance skips reads; exclusive SQLite lock handled off GUI; heartbeat continues; retry succeeds after release')
            logging.info('GUI_MAINTENANCE_ACCEPTANCE logging probe')
            for h in logging.getLogger().handlers:h.flush()
            require('GUI_MAINTENANCE_ACCEPTANCE' in (out/'settings/app.log').read_text('utf-8'),'log handler missing')
            require(not errors,'\n'.join(errors));checks.append('no unhandled Qt callbacks; timestamped log written in isolated settings')
            from version_info import diagnostic
            report={'passed':True,'frozen_exe':bool(getattr(sys,'frozen',False)),'checks':checks,'errors':errors,'export_files':[p.name for p in export_folder.glob('*.xlsx')],
                'heartbeat_ticks':len(beats),'max_gap_seconds':max((y-x for x,y in zip(beats,beats[1:])),default=0),**diagnostic()}
        except Exception:report={'passed':False,'frozen_exe':bool(getattr(sys,'frozen',False)),'checks':checks,'errors':errors,'error':traceback.format_exc()}
        finally:
            controller.stop();heartbeat.stop();sys.excepthook=old_hook
            export_locations.save_file,export_locations.directory=old_save,old_directory
            (out/'gui-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
            win._discard_close=True;app.exit(0 if report['passed'] else 1)
    QTimer.singleShot(200,execute)
