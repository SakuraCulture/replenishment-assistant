"""Real-window acceptance: edit parameters, explicit defaults, daily confirmation and restore."""
import copy,json,sys,time,traceback
from datetime import date,timedelta
from pathlib import Path
from PySide6.QtCore import Qt,QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication,QMessageBox,QInputDialog
import warehouse_catalog as wc,parameter_schema as ps,work_repository as wr,batch_service
from version_info import diagnostic

def start_smoke(app,win,args):
    fixture,out=map(Path,args[:2]);out.mkdir(parents=True,exist_ok=True);state={'step':0,'start':time.monotonic(),'checks':[],'moved':[]};timer=QTimer(win)
    def snap(name):win.repaint();assert win.grab().save(str(out/(name+'.png')))
    def finish(error=None):
        timer.stop()
        for source,target in state['moved']:
            if target.exists() and not source.exists():target.rename(source)
        meta=diagnostic();r={'passed':error is None,'error':error,'version':meta['version'],'source_commit':meta['source_commit'],'checks':state['checks']};(out/'work_ui_report.json').write_text(json.dumps(r,ensure_ascii=False,indent=2),encoding='utf-8');app.exit(1 if error else 0)
    win.failed=lambda message:finish(message);sys.excepthook=lambda t,v,tb:finish(''.join(traceback.format_exception(t,v,tb)))
    def overwrite(choice,name):
        def click():
            try:
                box=QApplication.activeModalWidget()
                if not (isinstance(box,QMessageBox) and box.objectName()=='dailyOverwrite'):
                    QTimer.singleShot(50,click);return
                assert box.defaultButton().text()=='取消';assert '原已完成现失效' in box.detailedText();assert box.grab().save(str(out/(name+'.png')))
                QTest.mouseClick(next(b for b in box.buttons() if b.text()==choice),Qt.MouseButton.LeftButton)
            except Exception:finish(traceback.format_exc())
        QTimer.singleShot(100,click)
    def tick():
        try:
            if time.monotonic()-state['start']>200:raise TimeoutError(str(state))
            if QApplication.activeModalWidget() is not None:return
            step=state['step']
            if step==0:
                for w in wc.WAREHOUSES:win.catalog_imported(wc.import_catalog(fixture/(w+'.xlsx'),w))
                win.sales.add_paths([str(fixture/'sales.xlsx')]);win.start_load();state['step']=1
            elif step==1 and win.worker is None:
                assert win.loaded
                # Loading disables the whole page; that must not clear logical selections.
                win.store_page.setEnabled(False);win.set_store_ui('QA_A',win.store_ui('QA_A'));assert win.store_widgets['QA_A']['check'].isChecked();win.store_page.setEnabled(True)
                resolved=copy.deepcopy(win.loaded);blocked=copy.deepcopy(resolved);blocked['blocked']['QA_A']=1;win.loaded_ok(blocked);assert not win.store_widgets['QA_A']['check'].isChecked()
                win.loaded_ok(resolved);assert set(win.selected_codes())==set(resolved['stores'])
                state['checks'].append('disabled loading page preserves logical selection; resolving blocked store restores eligible selection')
                for s,w in win.store_widgets.items():
                    w['mode'].setCurrentIndex(1);w['primary'].setCurrentIndex(w['primary'].findData('河北仓'));w['secondary'].setCurrentIndex(w['secondary'].findData('山东仓'))
                win.navigate(1);state['step']=10
                def edit():
                    try:
                        dialog=QApplication.activeModalWidget();assert hasattr(dialog,'param_table');dialog.param_table.cellWidget(list(ps.HELP).index('probability'),1).setValue(.5);assert dialog.grab().save(str(out/'01_完整超参.png'))
                        def name():
                            box=QApplication.activeModalWidget();assert isinstance(box,QInputDialog);box.setTextValue('界面验收方案');box.accept()
                        QTimer.singleShot(100,name);dialog.save_preset(False);assert dialog.preset['name']=='界面验收方案';dialog.finish('default')
                    except Exception:finish(traceback.format_exc())
                QTimer.singleShot(150,edit);win.edit_parameters(['QA_A'])
                assert win.work_repository.stores()['QA_A']['preset']['config']['parameters']['probability']==.5
                assert win.selected_settings('QA_B')['parameter_config']['parameters']['probability']==.9
                state['checks'].append('actual parameter editor and named full preset save; explicit store defaults; only selected store receives changed probability')
                win.start_calculation();state['step']=2
            elif step==2 and win.worker is None:
                assert all(st['state']=='已完成' for st in win.states.values()),win.states
                assert win.save_current();state['original']=win.work_repository.get_job(win.job_day);snap('02_首次每日保存')
                w=win.store_widgets['QA_A'];w['mode'].setCurrentIndex(2);w['amount'].setText('0');assert win.states['QA_A']['state']=='待重算';assert win.states['QA_B']['state']=='已完成'
                state['step']=20;overwrite('取消','03_取消覆盖');assert not win.save_current();assert win.work_repository.get_job(win.job_day)['revision']==state['original']['revision']
                overwrite('覆盖当天记录','04_确认覆盖');assert win.save_current();record=win.work_repository.get_job(win.job_day);assert record['revision']!=state['original']['revision'] and len(win.work_repository.list_jobs())==1
                assert record['summary']['completed']==1 and win.work_repository.stores()['QA_A']['ui']['mode']==1
                state['checks'].append('first save; same-day modal defaults to cancel; cancellation preserves original; explicit replace retains one record and flags stale store; saving job never alters store defaults')
                # Re-checking the same inputs restores saved full defaults, rather than resetting parameters.
                from workbench import Workbench
                defaults=Workbench();defaults.loaded_ok(copy.deepcopy(win.loaded));assert defaults.selected_settings('QA_A')['parameter_config']['parameters']['probability']==.5;assert defaults.store_widgets['QA_A']['mode'].currentIndex()==1;defaults._discard_close=True;defaults.close()
                win.loaded_ok(copy.deepcopy(win.loaded));assert win.selected_settings('QA_A')['parameter_config']['parameters']['probability']==.5;assert win.store_widgets['QA_A']['mode'].currentIndex()==2
                win.restore_job(record);assert not win.job_dirty and win.store_widgets['QA_A']['mode'].currentIndex()==2
                assert win.save_current() and win.work_repository.get_job(win.job_day)['revision']==record['revision']
                for filename in ['sales.xlsx','河北仓.xlsx','山东仓.xlsx']:
                    source=fixture/filename;target=fixture/(filename+'.ui-moved');source.rename(target);state['moved'].append((source,target))
                from workbench import Workbench
                restored=Workbench();restored.restore_job(record);assert restored.selected_settings('QA_A')['budget']==0 and len(restored.loaded['normalized'])==24 and not restored.job_dirty;restored.close()
                win.store_widgets['QA_B']['check'].setChecked(False);win.start_calculation();state['step']=3
            elif step==3 and win.worker is None:
                assert win.states['QA_A']['state']=='已完成' and win.results['QA_A']['summary']['amount']==0
                win.navigate(3);snap('05_快照恢复重算');win.job(batch_service.export_batch,(win.results,win.states,out/'快照导出.xlsx'),lambda p:state.update(exported=p));state['step']=4
            elif step==4 and win.worker is None:
                assert Path(state['exported']).is_file();state['checks'].append('saved store parameters survive re-import; fresh window restores daily snapshot; recalculation and export work with source Excel files moved away')
                payload=copy.deepcopy(win.snapshot());day=(date.fromisoformat(wr.today())-timedelta(days=1)).isoformat();payload['day']=day;win.work_repository.save_job(day,payload)
                from workbench import Workbench
                historical=Workbench();historical.restore_job(win.work_repository.get_job(day));assert historical.readonly and not historical.save_job_btn.isEnabled() and not historical.calculate_btn.isEnabled();historical.close()
                state['checks'].append('historical date opens read-only; snapshot not changed by current defaults')
                win.resize(1080,720);win.navigate(1);state['step']=5
            elif step==5:
                snap('06_小窗口配置');state['step']=50
                def cancel_close():
                    box=QApplication.activeModalWidget();assert isinstance(box,QMessageBox);QTest.mouseClick(next(b for b in box.buttons() if b.text()=='取消'),Qt.MouseButton.LeftButton)
                assert win.job_dirty;QTimer.singleShot(100,cancel_close);win.close();assert win.isVisible();state['checks'].append('closing unsaved work asks; cancel keeps current results and window');finish()
        except Exception:finish(traceback.format_exc())
    timer.timeout.connect(tick);timer.start(250)
