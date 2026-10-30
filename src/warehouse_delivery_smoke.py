"""Opt-in source/EXE UI acceptance; every write uses launcher-owned QA paths."""
import copy,json,time,traceback
from pathlib import Path
from PySide6.QtCore import QTimer,QEventLoop,Qt
from PySide6.QtWidgets import QApplication,QDialog,QFileDialog,QMessageBox,QPushButton,QCheckBox
from version_info import diagnostic
import work_repository as wr,warehouse_catalog as wc,parameter_schema as schema

def start_smoke(app,win,args):
    fixture,out=map(Path,args[:2]);phase=args[2] if len(args)>2 else 'exercise';checks=[];shots=[];state={};last=[time.perf_counter()];gaps=[]
    def wait(predicate,seconds=60):
        loop=QEventLoop();timer=QTimer();start=time.monotonic();timer.timeout.connect(lambda:loop.quit() if predicate() or time.monotonic()-start>seconds else None);timer.start(20);loop.exec();timer.stop();assert predicate(),'UI wait timed out'
    def shot(widget,name):widget.grab().save(str(out/name));shots.append(name)
    def automate():
        now=time.perf_counter();gaps.append(now-last[0]);last[0]=now;dialog=QApplication.activeModalWidget()
        if not dialog:return
        mode=state.get('mode')
        if dialog.objectName()=='merchantSelection' and mode in ('export','history','selection_cancel'):
            shot(dialog,'warehouse-selection-'+mode+'.png')
            if mode=='selection_cancel':dialog.reject();return
            if mode=='history':
                dialog.select_all(False)
                for s,w,c,valid in dialog.items:
                    if w=='山东仓' and valid:c.setCheckState(Qt.CheckState.Checked);break
            dialog.accept()
        elif dialog.objectName()=='deliveryConfirm' and mode=='export':
            assert '分仓' in dialog.text() and dialog.defaultButton().text()=='取消';shot(dialog,'sign-preview.png')
            next(b for b in dialog.buttons() if b.text()=='确认并导出').click()
        elif dialog.objectName()=='storeDefaultsConfirm':
            assert not dialog.findChild(QCheckBox).isChecked();shot(dialog,'save-defaults.png');dialog.accept()
        elif isinstance(dialog,QDialog) and dialog.windowTitle().startswith('确认保存门店回传'):
            shot(dialog,'return-preview.png');dialog.accept()
    timer=QTimer(win);timer.setInterval(30);timer.timeout.connect(automate);timer.start()
    def finish(error=None):
        timer.stop();report={'passed':error is None,'error':error,'checks':checks,'screenshots':shots,'version':diagnostic(),'phase':phase,'max_ui_gap_seconds':max(gaps,default=0),'ui_ticks':len(gaps)}
        (out/('ui-'+phase+'.json')).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');win.job_dirty=False;win._discard_close=True;win.close();app.exit(0 if error is None else 1)
    def exercise():
        try:
            from merchant_selection import MerchantSelection
            from parameter_ui import ParameterDialog
            from feedback_ui import import_returns,FeedbackDialog
            from ui_work_io import Session,request
            from normal_delivery import job_head
            from import_ui import ImportDialog
            import normal_exports as ne,warehouse_orders as wo,merchant_feedback as mf,openpyxl
            root=fixture.parent
            if phase=='reopen':
                win.sales.paths=[str(root/'sales.xlsx')];win.sales.refresh();win.start_load();wait(lambda:win.worker is None)
                saved=wr.loads((out/'saved-settings.json').read_bytes())
                for s,record in saved.items():
                    assert win.config_for(s)==record['preset']['config'];assert all(win.store_ui(s)[k]==record['ui'][k] for k in ('primary','secondary','mode','amount'));assert '已保存默认' in win.store_widgets[s]['params'].text()
                checks.append('second actual process: import same store codes automatically restores primary/secondary, named warehouse cycles and full parameters')
                job=win.work_repository.get_job(wr.today());win.restore_job(job)
                original=wr.loads((out/'historical-settings.json').read_bytes())
                for s in original:assert win.config_for(s)==original[s]
                checks.append('restoring saved daily snapshot uses original parameters, not subsequently saved defaults');shot(win,'restart-restore.png')
                from archive_smoke import exercise as archive_checks
                archive_checks(win,out,checks,shot,wait,phase);finish();return
            for wh in wc.WAREHOUSES:
                win.import_catalog_path(str(root/(wh+'.xlsx')),wh);wait(lambda:win.worker is None);assert wh in win.catalogs
            win.sales.paths=[str(root/'sales.xlsx')];win.sales.refresh();win.start_load();wait(lambda:win.worker is None);assert list(win.store_widgets)==['QA_A','QA_B'];checks.append('packaged background imports/check-file spawn path completed for two stores and two warehouse catalogs')
            for i,s in enumerate(win.store_widgets):
                widgets=win.store_widgets[s]
                for key,value in [('primary','河北仓' if i==0 else '山东仓'),('secondary','山东仓' if i==0 else '河北仓')]:widgets[key].setCurrentIndex(widgets[key].findData(value))
                widgets['mode'].setCurrentIndex(1)
                config=win.config_for(s);config['warehouses']={'河北仓':[9+i,4],'山东仓':[11+i,5]};config['parameters']['moq_price']=22+i
                win.apply_parameters([s],config,win.preset_for(s),True)
            win.save_default_dialog();saved=win.work_repository.stores();assert all(v['ui']['mode']==0 for v in saved.values())
            checks.append('actual save-default dialog persists effective config and routes; unchecked budget option does not save temporary unlimited budget')
            # Calculate using actual store settings before the export fixture is installed.
            win.start_calculation();wait(lambda:win.worker is None,90);assert all(st['state']=='已完成' for st in win.states.values());checks.append('actual packaged two-store computation with separate parameters completes')
            # Deliberately use frozen accepted synthetic result fixture for return/zero-new scope checks.
            p=wr.loads(fixture.read_bytes());p['day']=wr.today();win.results=copy.deepcopy(p['results']);win.states=copy.deepcopy(p['states']);win.new_focus=copy.deepcopy(p['new_focus']);win.input_dirty=False;win.refresh_tables()
            historical={s:win.config_for(s) for s in win.store_widgets};(out/'historical-settings.json').write_text(wr.dumps(historical),encoding='utf-8')
            selection=MerchantSelection(win);assert len(selection.items)==4 and list(selection.warehouses())==['QA_A','QA_B'];selection.select_all(False);assert not selection.ok.isEnabled();selection.reject();selection.deleteLater()
            state['mode']='selection_cancel';win.export_current();assert not win.orders.headers();checks.append('store/warehouse selector defaults all four children; empty selection disabled; cancel does not sign')
            original_dir=QFileDialog.getExistingDirectory;original_save=QFileDialog.getSaveFileName;folder=out/'exports';folder.mkdir();QFileDialog.getExistingDirectory=lambda *a,**k:str(folder);state['mode']='export';win.export_current();state['mode']=None
            files=list(folder.glob('*.xlsx'));assert len(files)==4 and len(win.orders.headers())==2;assert all('（精简版·' in f.name for f in files);checks.append('actual batch UI signs two parent orders and exports four warehouse files through frozen spawn IO process')
            orders=[win.orders.get(wr.today(),s) for s in ('QA_A','QA_B')];snapshots=[wr.dumps(o['payload']) for o in orders]
            for f in files:
                b=openpyxl.load_workbook(f);props={x.name:x.value for x in b.custom_doc_props};wh=props['供货仓'];assert props['补货单号'].endswith('-'+wo.SUFFIXES[wh]);assert all(r[18].value==wh and r[20].value==props['补货单号'] for ws in b for r in list(ws.rows)[1:]);b.close()
            # Actual UI feedback preview/save. Choose all four child files with the import dialog seam.
            import import_ui
            choose=import_ui.choose;info=QMessageBox.information;import_ui.choose=lambda *a,**k:[str(f) for f in files];QMessageBox.information=lambda *a,**k:QMessageBox.StandardButton.Ok
            receipts=import_returns(win,win);import_ui.choose=choose;QMessageBox.information=info;assert len(receipts)==2;assert all(len(r['warehouses'])==2 for r in receipts)
            head={k:v for k,v in orders[0].items() if k!='payload'};view=FeedbackDialog(win,win,head);view.show();assert '河北仓' in view.note.text() and '山东仓' in view.note.text();assert view.model.columnCount()==8;shot(view,'warehouse-feedback.png');view.reject();view.deleteLater()
            checks.append('actual return preview/save accepts both child IDs, displays warehouse statuses and preserves signed quantities')
            hfile=out/'history-sd.xlsx';QFileDialog.getSaveFileName=lambda *a,**k:(str(hfile),'');state['mode']='history';win.export_loaded_order(orders[0]);wait(lambda:hfile.exists() and win.worker is None);state['mode']=None
            assert [wr.dumps(win.orders.get(wr.today(),o['store'],1)['payload']) for o in orders]==snapshots;checks.append('history selector exports only Shandong from exact V001; original snapshots unchanged')
            # Save changed defaults after daily/archive snapshots to demonstrate historical isolation on reopen.
            for s in win.store_widgets:
                config=win.config_for(s);config['warehouses']['山东仓'][0]+=1;win.apply_parameters([s],config,win.preset_for(s),True)
            (out/'saved-settings.json').write_text(wr.dumps(win.work_repository.stores()),encoding='utf-8')
            c=win.config_for('QA_A');c['warehouses']['河北仓'][0]+=3;win.apply_parameters(['QA_A'],c,win.preset_for('QA_A'),False);assert '仅本次' in win.store_widgets['QA_A']['params'].text();assert win.work_repository.stores()['QA_A']['preset']['config']!=win.config_for('QA_A');checks.append('temporary edits are visibly temporary and do not overwrite defaults; reopen tests new defaults vs historical snapshot')
            QFileDialog.getExistingDirectory=original_dir;QFileDialog.getSaveFileName=original_save
            from archive_smoke import exercise as archive_checks
            archive_checks(win,out,checks,shot,wait,phase);finish()
        except Exception:finish(traceback.format_exc())
    QTimer.singleShot(100,exercise)
