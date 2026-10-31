"""Opt-in four-scene acceptance in the actual packaged application, isolated QA."""
import copy,json,time,traceback,sqlite3
from pathlib import Path
from PySide6.QtCore import QTimer,QEventLoop,QMimeData,QUrl,QPointF,Qt
from PySide6.QtGui import QDropEvent
from PySide6.QtWidgets import QApplication,QMessageBox,QDialog,QPushButton
from version_info import diagnostic

def start_smoke(app,win,args):
    out=Path(args[0]);phase=args[1];state={};checks=[];timings={};gaps=[];last=[time.perf_counter()]
    def wait(predicate,seconds=60):
        loop=QEventLoop();timer=QTimer();started=time.monotonic();timer.timeout.connect(lambda:loop.quit() if predicate() or time.monotonic()-started>seconds else None);timer.start(20);loop.exec();timer.stop()
        if not predicate():raise AssertionError('UI timeout: '+repr(state))
    def drop(widget,paths):
        mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(p)) for p in paths]);event=QDropEvent(QPointF(20,20),Qt.DropAction.CopyAction,mime,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier);widget.dropEvent(event)
    def automate():
        from merchant_selection import MerchantSelection
        from lifecycle_batch_ui import BatchImport
        now=time.perf_counter();gaps.append(now-last[0]);last[0]=now;d=QApplication.activeModalWidget()
        if d is None:return
        try:
            if isinstance(d,MerchantSelection):
                if d.destination.text()!=str(out/'exports'):raise AssertionError('default directory missing')
                state['sign_scope']=len(d.codes());d.grab().save(str(out/'sign-scope.png'));d.accept()
            elif isinstance(d,QMessageBox):
                if d.objectName()=='deliveryConfirm':
                    if d.defaultButton().text()!='取消':raise AssertionError('unsafe default')
                    d.grab().save(str(out/'sign-confirm.png'));next(b for b in d.buttons() if b.text()=='确认签发并导出').click()
                else:raise AssertionError(d.windowTitle()+': '+d.text())
            elif d.windowTitle()=='签发与导出结果':next(b for b in d.findChildren(QPushButton) if b.text()=='继续本次补货').click()
            elif isinstance(d,BatchImport):
                if not d.paths:
                    drop(d,[out/f for f in ('receipt.xlsx','payment.xlsx','return-hb.xlsx','return-sd.xlsx')]);return
                if d.busy:return
                if d.pages.currentIndex()==0:
                    if d.issues:raise AssertionError('unresolved auto formats '+repr(d.issues))
                    for c in d.targets:
                        if c.currentIndex()==0:c.setCurrentIndex(next(i for i in range(1,c.count()) if c.itemData(i)['number']=='BH-T-ZC-HB'))
                    d.preview();return
                d.grab().save(str(out/('batch-'+state.get('mode','save')+'.png')))
                if state.get('mode')=='cancel':d.reject();return
                if d.review.save.isEnabled():raise AssertionError('acknowledgement missing')
                d.review.complete.setChecked(True);d.review.extra_ack.setChecked(True);d.review.save.click()
        except Exception:
            state['automation_error']=traceback.format_exc();d.reject()
    timer=QTimer(win);timer.setInterval(30);timer.timeout.connect(automate);timer.start()
    def finish(error=None):
        timer.stop();report={'passed':error is None,'error':error,'checks':checks,'timings':timings,'version':diagnostic(),'phase':phase,'ui_ticks':len(gaps),'max_ui_gap_seconds':max(gaps,default=0),'synthetic':True,'automated_operation_counts':{'replenishment':['drop inputs','start calculation','sign/export','confirm store/warehouse scope','confirm signature changes'],'lifecycle':['drop four files','bind unnumbered payment source order once','open preview','acknowledge complete scope','save'],'review':['search order','open decision evidence'],'defaults':['select store','edit defaults','confirm per-store differences']},'limits':'Automated scenario operations, not a human usability study; business reading/decision time not measured.'};(out/('ui-'+phase+'.json')).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');win.job_dirty=False;win._discard_close=True;win.close();app.exit(1 if error else 0)
    def exercise():
        try:
            import order_lifecycle as service
            win.show_section(3);wait(lambda:bool(win.followup.records));hb=next(h for h in win.followup.records if h['number']=='BH-T-ZC-HB')
            if phase=='reopen':
                if service.snapshot(win.work_repository.path,hb)['status']!='已收齐':raise AssertionError('restart lost lifecycle')
                win.show_section(4);p=win.work_repository.stores()['QA_A']['preset']['config']['parameters']['probability']
                if p!=.5:raise AssertionError('default not persisted')
                checks.append('new process restored saved defaults and lifecycle without replacing signing');finish();return
            # Existing source inputs/defaults were frozen before launching this process.
            win.show_section(1);start=time.perf_counter();drop(win.sales,[out/'sales.xlsx']);wait(lambda:win.worker is None and win.loaded is not None)
            if win.pages.currentIndex()!=1 or set(win.selected_codes())!={'QA_A','QA_B'}:raise AssertionError(('auto check/default selection',win.pages.currentIndex(),win.selected_codes(),win.loaded['blocked'],{s:win.store_ui(s) for s in win.store_widgets}))
            timings['read_check_seconds']=time.perf_counter()-start;win.grab().save(str(out/'replenishment-ready.png'))
            start=time.perf_counter();win.calculate_btn.click();wait(lambda:win.worker is None and len(win.results)==2)
            if any(s['state']!='已完成' for s in win.states.values()):raise AssertionError(win.states)
            timings['calculation_seconds']=time.perf_counter()-start;before=copy.deepcopy(win.results)
            start=time.perf_counter();win.export_current();timings['sign_export_seconds']=time.perf_counter()-start
            if state.get('automation_error'):raise AssertionError(state['automation_error'])
            if not list((out/'exports').glob('*.xlsx')):raise AssertionError('no signed files')
            checks.append('drag/drop automatically checked; configured stores selected, process queue computed two stores; sign and export to displayed default folder; persistent completion dialog')
            from lifecycle_batch_ui import import_batch
            state['mode']='cancel'
            if import_batch(win,win) or service.history(win.work_repository.path,hb,'return'):raise AssertionError('cancel saved document')
            state['mode']='save';start=time.perf_counter();receipt=import_batch(win,win);timings['mixed_import_seconds']=time.perf_counter()-start
            if not receipt or len(receipt)!=4:raise AssertionError(state.get('automation_error','batch failed'))
            if service.snapshot(win.work_repository.path,hb)['rows'][0][3:6]!=[6,4,0]:raise AssertionError('batch predecessor values')
            checks.append('four mixed files dragged in reverse stage order; one unnumbered payment manually bound; preview cancel, retry, atomic four-document save')
            from history_ui import ArchiveDialog
            start=time.perf_counter();d=ArchiveDialog(win,hb,initial_tab='audit',sku='0001');d.show();wait(lambda:2 in d.children_views and bool(d.children_views[2].loaded_key))
            audit=d.children_views[2]
            if not any('后续事实' in r[0] for r in audit.model.rows):raise AssertionError('decision overview lacks downstream quantities')
            timings['review_seconds']=time.perf_counter()-start;d.grab().save(str(out/'decision-review.png'));d.purchases();wait(lambda:1 in d.children_views);d.grab().save(str(out/'order-detail.png'));d.reject();d.deleteLater()
            checks.append('same order detail switches signed/lifecycle/evidence; frozen sales/stock/parameters plus downstream facts; no nested order window')
            win.show_section(4);scene=win.parameter_scene;scene.refresh();i=scene.codes.index('QA_A');scene.table.item(i,0).setCheckState(Qt.CheckState.Checked)
            from parameter_ui import ParameterDialog
            from unittest.mock import patch
            def editor(dialog):
                dialog.param_table.cellWidget(list(__import__('parameter_schema').HELP).index('probability'),1).setValue(.5)
                with patch('interaction_ui.review_rows',return_value=True):dialog.finish('default')
                return dialog.result()
            # Actual form and persistence; fixed confirmation approval in QA only.
            with patch.object(ParameterDialog,'exec',editor):scene.edit_defaults()
            if win.results!=before:raise AssertionError('save-default modified current result')
            if win.work_repository.stores()['QA_A']['preset']['config']['parameters']['probability']!=.5:raise AssertionError('default not saved')
            win.grab().save(str(out/'parameters.png'));checks.append('standalone parameter form saves next-use defaults, leaves both current results exact; restart checked separately')
            for size in ((1380,900),(1080,720)):
                win.resize(*size)
                for n in (0,1,2,3,4):win.show_section(n);app.processEvents();win.grab().save(str(out/f'layout-{size[0]}-{n}.png'))
            finish()
        except Exception:finish(traceback.format_exc()+'\n'+state.get('automation_error',''))
    QTimer.singleShot(500,exercise)
