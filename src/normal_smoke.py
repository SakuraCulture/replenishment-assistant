"""Opt-in actual Qt/EXE acceptance; private fixtures/config supplied by launcher."""
import copy,json,sys,time,traceback
from pathlib import Path
from PySide6.QtCore import Qt,QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication,QMessageBox,QFileDialog,QLineEdit
import warehouse_catalog as wc,work_repository as wr,normal_orders
from new_product_ui import NewProductDialog
from version_info import diagnostic


def start_smoke(app,win,args):
    fixture,out=[Path(p).resolve() for p in args[:2]];out.mkdir(parents=True,exist_ok=True);state={'step':0,'start':time.monotonic(),'checks':[]};timer=QTimer(win)
    app.setAttribute(Qt.ApplicationAttribute.AA_DontUseNativeDialogs,True)
    def snap(name):win.repaint();assert win.grab().save(str(out/(name+'.png')))
    def finish(error=None):
        timer.stop();meta=diagnostic();r={'passed':error is None,'error':error,'version':meta['version'],'source_commit':meta['source_commit'],'checks':state['checks']};(out/'normal_ui_report.json').write_text(json.dumps(r,ensure_ascii=False,indent=2),encoding='utf-8')
        if error:app.exit(1);return
        # Exercise the same accepted-close path as an operator, not a forced
        # event-loop exit while modal/worker callbacks are still unwinding.
        def close():
            def discard():
                box=QApplication.activeModalWidget()
                if isinstance(box,QMessageBox):QTest.mouseClick(next(b for b in box.buttons() if b.text()=='放弃本次变更'),Qt.MouseButton.LeftButton)
            QTimer.singleShot(150,discard);win.close()
        QTimer.singleShot(0,close)
    win.failed=lambda message:finish(message);sys.excepthook=lambda t,v,tb:finish(''.join(traceback.format_exception(t,v,tb)))
    def later(fn):
        def guarded():
            try:fn()
            except Exception:finish(traceback.format_exc())
        QTimer.singleShot(150,guarded)
    def modal_click(name,text):
        box=QApplication.activeModalWidget();assert isinstance(box,QMessageBox) and box.objectName()==name;assert box.defaultButton().text()=='取消';assert box.grab().save(str(out/(name+'_'+text+'.png')));QTest.mouseClick(next(b for b in box.buttons() if b.text()==text),Qt.MouseButton.LeftButton)
    def select_new(amount=6,accept=True):
        dialog=QApplication.activeModalWidget();assert isinstance(dialog,NewProductDialog)
        i=next(i for i,d in enumerate(dialog.rows) if d['E']=='new_must');check,qty=dialog.widgets[i];qty.setValue(amount);check.setChecked(True)
        dialog.search.setText('new_plain');assert check.isChecked() and dialog.table.isRowHidden(i);assert '筛选外已选 1' in dialog.note.text();dialog.search.clear();assert dialog.grab().save(str(out/'新品选择.png'))
        later(lambda:modal_click('newBudgetPreview','应用到本店草稿' if accept else '取消'))
        QTest.mouseClick(dialog.preview_btn,Qt.MouseButton.LeftButton)
    def tick():
        try:
            if time.monotonic()-state['start']>240:raise TimeoutError(str(state))
            if QApplication.activeModalWidget() is not None:return
            step=state['step']
            if step==0:
                assert win.sections.currentIndex()==0;snap('01_首页');QTest.mouseClick(win.top_nav[1],Qt.MouseButton.LeftButton);assert win.sections.currentIndex()==1
                for w in wc.WAREHOUSES:win.catalog_imported(wc.import_catalog(fixture/(w+'.xlsx'),w))
                win.profile=json.loads((fixture/'must.json').read_text(encoding='utf-8'));win.repository.save(win.profile);win.refresh_profile_label();win.sales.add_paths([str(fixture/'sales.xlsx')]);win.start_load();state['step']=1
            elif step==1 and win.worker is None:
                assert len(win.loaded['normalized'])==22
                for s,w in win.store_widgets.items():
                    w['mode'].setCurrentIndex(2);w['amount'].setText('12');w['primary'].setCurrentIndex(w['primary'].findData('河北仓'));w['secondary'].setCurrentIndex(w['secondary'].findData('山东仓'))
                win.start_calculation();state['step']=2
            elif step==2 and win.worker is None:
                assert all(x['state']=='已完成' for x in win.states.values()),win.states
                state['original']=copy.deepcopy(win.results);win.current_store='QA_A';win.navigate(3);state['step']=20
                later(lambda:select_new(6,False));win.choose_new_products();state['step']=3
            elif step==3 and win.worker is None:
                assert win.results==state['original'];state['checks'].append('real file check, two-store spawn calculation; filtered selection retained; budget preview cancel preserves both stores')
                state['step']=30;later(lambda:select_new(6,True));win.choose_new_products();state['step']=4
            elif step==4 and win.worker is None:
                assert win.results['QA_A']['new_products']['selection']=={'new_must':6.0};assert win.results['QA_B']==state['original']['QA_B'];assert win.results['QA_A']['summary']['must_amount']==12
                win.mode.setCurrentText('新品推荐');assert win.proxy.rowCount()==1;snap('02_新品预算执行');state['step']=40
                later(lambda:modal_click('normalConfirm','取消'));win.confirm_normal();assert not win.orders.list()
                later(lambda:modal_click('normalConfirm','确认并归档'));win.confirm_normal();assert len(win.orders.list())==2
                state['order']=win.orders.get(wr.today(),'QA_A');assert state['order']['revision']==1 and not win.job_dirty
                later(lambda:modal_click('normalConfirm','确认并归档'));win.confirm_normal();assert win.orders.get(wr.today(),'QA_A')['revision']==1
                state['checks'].append('mandatory new item takes shared budget ahead of ordinary sales; other store unchanged; new filter; first confirmation cancel/accept; auto daily save; duplicate confirm idempotent')
                # Prepare a changed first-fill candidate; execution may stay capped by budget.
                later(lambda:select_new(12,True));win.choose_new_products();state['step']=5
            elif step==5 and win.worker is None:
                assert win.orders.get(wr.today(),'QA_A')['revision']==1;state['step']=50
                later(lambda:modal_click('normalConfirm','取消'));win.confirm_normal();assert win.orders.get(wr.today(),'QA_A')['revision']==1
                later(lambda:modal_click('normalConfirm','确认并归档'));win.confirm_normal();order=win.orders.get(wr.today(),'QA_A');assert order['revision']==2 and order['number']==state['order']['number'];assert win.orders.get(wr.today(),'QA_B')['revision']==1
                assert win.orders.get(wr.today(),'QA_A',1)['payload']['result']['new_products']['selection']['new_must']==6
                state['checks'].append('changed candidate leaves archived V001 until explicit overwrite; default-cancel; same number V002; unchanged other store remains V001; old revision readable')
                def choose_file():
                    dialog=QApplication.activeModalWidget();assert isinstance(dialog,QFileDialog);dialog.setDirectory(str(out));dialog.findChild(QLineEdit,'fileNameEdit').setText('EXE确认单.xlsx');assert Path(dialog.selectedFiles()[0])==out/'EXE确认单.xlsx';dialog.accept()
                later(choose_file);win.export_current();state['step']=6
            elif step==6 and win.worker is None:
                assert (out/'EXE确认单.xlsx').is_file();assert any(e['action']=='带号导出' for e in win.orders.events())
                record=win.work_repository.get_job(wr.today());state['record']=record;win.reset_job();assert not win.new_bases;win.restore_job(record);assert 'QA_A' in win.new_bases and win.results['QA_A']['new_products']['selection']['new_must']==12
                state['checks'].append('actual file-save dialog and frozen numbered Excel export; queryable event; saved new choices and original base restored after resetting work area')
                win.show_section(2);win.archive_search.setText('new_must');assert not win.archive_table.isRowHidden(0);snap('03_归档查询');state['step']=60
                def inspect_archive():
                    dialog=QApplication.activeModalWidget();assert dialog is not None;assert dialog.grab().save(str(out/'04_修订明细.png'));dialog.accept()
                later(inspect_archive);win.open_archive(win.archive_orders[0])
                win.show_section(1);win.store_widgets['QA_A']['amount'].setText('0');assert win.states['QA_A']['state']=='待重算';assert win.orders.get(wr.today(),'QA_A')['revision']==2
                win.current_store='QA_A';assert not win.orders.get(wr.today(),'QA_A')['payload']['result']['budget']['limit']==0
                # Recalculation from the saved immutable input snapshot retains standard behavior.
                later(lambda:modal_click('newSelectionReset','重新计算并重选新品'));win.start_calculation(['QA_A']);state['step']=7
            elif step==7 and win.worker is None:
                assert win.results['QA_A']['summary']['amount']==0 and 'QA_A' not in win.new_bases and not win.results['QA_A'].get('new_products')
                assert win.orders.get(wr.today(),'QA_A')['revision']==2
                state['checks'].append('history query/detail independent of work area; parameter changes invalidate only selected store; rerun resets selections and leaves issued order untouched')
                win.show_section(0);snap('05_首页归档状态');state['step']=70
                def cancel_close():
                    box=QApplication.activeModalWidget();assert isinstance(box,QMessageBox);assert box.defaultButton().text()=='取消';QTest.mouseClick(next(b for b in box.buttons() if b.text()=='取消'),Qt.MouseButton.LeftButton)
                later(cancel_close);win.close();assert win.isVisible();state['checks'].append('home navigation retains unsaved work; close cancel keeps application alive');finish()
        except Exception:finish(traceback.format_exc())
    timer.timeout.connect(tick);timer.start(250)
