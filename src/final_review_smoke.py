"""Opt-in actual Qt editor acceptance used by the packaged preissue smoke."""
import copy,time
from PySide6.QtCore import Qt,QTimer
from PySide6.QtTest import QTest,QAbstractItemModelTester
from PySide6.QtWidgets import QApplication,QTableView
from preissue_ui import FinalListDialog,PackInput
import result_semantics as sem
import preissue

def exercise(host,store,sku,out,wait):
    before=copy.deepcopy(host.results);errors=[]
    def excluded_check():
        dialog=QApplication.activeModalWidget()
        try:
            table=dialog.findChild(QTableView);p=table.model()
            if p.rowCount()==0 or any(not sem.exclusion_reason(p.sourceModel().rows[p.mapToSource(p.index(i,0)).row()]) for i in range(p.rowCount())):raise AssertionError('read-only excluded scope incorrect')
            if table.editTriggers()!=QTableView.EditTrigger.NoEditTriggers:raise AssertionError('excluded view editable')
            dialog.grab().save(str(out/'read-only-excluded.png'))
        except Exception as exc:errors.append(repr(exc))
        finally:
            if dialog:dialog.accept()
    QTimer.singleShot(100,excluded_check);host.show_excluded()
    if errors:raise AssertionError(errors)
    d=FinalListDialog(host,list(host.results));d.show();QApplication.processEvents()
    tests=[QAbstractItemModelTester(m,QAbstractItemModelTester.FailureReportingMode.Fatal) for m in (d.model,d.proxy)]
    times=[]
    try:
        d.reason.setCurrentText('经营判断');i=d.model.positions[(store,sku)];step=d.model.entries[i][1]['BD'];d.stores.setCurrentIndex(d.stores.findData(store))
        for n in range(30):
            d.scope.setCurrentText('可调整');d.filter();QApplication.processEvents();d.table.sortByColumn(9,Qt.SortOrder.DescendingOrder if n%2 else Qt.SortOrder.AscendingOrder)
            index=d.proxy.mapFromSource(d.model.index(i,9));d.table.setCurrentIndex(index);d.table.selectRow(index.row());d.table.edit(index);QApplication.processEvents()
            editor=d.table.findChild(PackInput)
            if editor is None:raise AssertionError('real quantity editor absent')
            q=step*(n%3);start=time.perf_counter();editor.lineEdit().selectAll();QTest.keyClicks(editor.lineEdit(),str(q))
            if n%3==0:
                QTest.keyClick(editor.lineEdit(),Qt.Key.Key_Tab);QTest.qWait(15)
                if d.table.state()==d.table.State.EditingState:QTest.keyClick(QApplication.focusWidget(),Qt.Key.Key_Return)
            elif n%3==1:QTest.mouseClick(d.search,Qt.MouseButton.LeftButton)
            else:QTest.keyClick(editor.lineEdit(),Qt.Key.Key_Return)
            wait(lambda:d.table.state()!=d.table.State.EditingState,5);QApplication.processEvents()
            d.check_visible(True);d.clear_checks();d.table.viewport().repaint();d.table.horizontalHeader().repaint();QApplication.processEvents()
            if d.model.edits[store][sku]['quantity']!=q:raise AssertionError('edited another SKU or quantity lost')
            times.append(time.perf_counter()-start)
        # Existing core preview is the independent oracle for read-only money.
        p=preissue.preview(host.results[store],d.model.edits[store],host.job_day,lists=d.model.lists[store]);m=d.model.money.summary(store)
        if float(m['total'])!=p['result']['summary']['amount']:raise AssertionError('live store money differs from core preview')
        d.grab().save(str(out/'final-live-money.png'))
        d.facets.more_btn.click();d.facets.fields['group'].setCurrentText('往期新品');QApplication.processEvents()
        if d.proxy.rowCount()!=1:raise AssertionError('expected one synthetic previous-month new candidate')
        d.facets.fields['adjustment'].setCurrentText('人工改量');QApplication.processEvents()
        if d.proxy.rowCount()!=0:raise AssertionError('independent group and adjustment filters did not intersect')
        d.grab().save(str(out/'combined-filters.png'));d.facets.reset();QApplication.processEvents()
        d.scope.setCurrentText('风险待确认');d.preview();wait(lambda:d.task is None)
        d.confirm_all.setChecked(True);QApplication.processEvents()
        if d.proxy.rowCount():raise AssertionError('confirmed risk still pending')
        d.model.setData(d.model.index(i,9),0);QApplication.processEvents()
        if d.prepared is not None or d.model.risks or d.apply_btn.isEnabled():raise AssertionError('stale preview/risk survived new edit')
        d.reset_filters();d.check_visible(True);d.search.setText('__no_matches__');d.filter()
        if d.chosen():raise AssertionError('hidden checked rows included in visible-only bulk')
        d.selection_scope.setCurrentIndex(1)
        if not d.chosen():raise AssertionError('explicit all checked selection unavailable')
        if host.results!=before:raise AssertionError('editor preview modified published business results')
        return {'passed':True,'native_quantity_edits':len(times),'input_methods':['Tab','mouse focus-out','Enter'],'sort_directions':2,'edit_seconds':times,'money_matches_core':True,'stale_risks_cleared':True,'bulk_scope_isolated':True,'read_only_excluded':True,'combined_filters':True,'host_results_unchanged':True}
    finally:d.dirty=False;d.reject();d.deleteLater();QApplication.processEvents()
