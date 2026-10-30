"""Opt-in QA extension only: isolated source and frozen EXE interaction contracts."""
import copy
from contextlib import closing
from unittest.mock import patch
from PySide6.QtCore import Qt,QTimer
from PySide6.QtWidgets import QApplication,QPushButton,QDialog,QCheckBox
from PySide6.QtTest import QTest


def exercise(win,out,checks,shot,wait):
    from parameter_ui import ParameterDialog
    from history_ui import ArchiveDialog,AuditDialog,EventsDialog
    from history_forms import events
    from interaction_ui import choose_stores
    from feedback_ui import import_returns
    from ui_work_io import Progress
    from new_product_ui import NewProductDialog
    import work_repository as wr
    # Current filtered/all selection scopes must differ without touching settings.
    win.show_section(1);win.navigate(1);win.select_all(False);win.store_search.setText('QA_A');win.select_all(True,True)
    if win.selected_codes()!=['QA_A']:raise AssertionError('visible-only select included hidden store')
    win.select_all(True)
    if win.selected_codes()!=['QA_A','QA_B'] or '隐藏已选 1' not in win.selection_note.text():raise AssertionError('hidden selection not explicit')
    shot(win,'interaction-store-scope.png');win.store_search.clear()
    s='QA_A';config=copy.deepcopy(win.config_for(s));persisted=wr.dumps(win.work_repository.stores())
    d=ParameterDialog(win,config,win.preset_for(s),scope='所选 1 家门店',store_configs={s:config});d.show()
    names=[b.text() for b in d.findChildren(QPushButton)]
    if '保存方案新版本' in names or '复制另存方案' in names or not d.retire_btn.isHidden():raise AssertionError('store editor contains immediate global writes')
    d.param_table.cellWidget(2,1).setValue(22)
    with patch('interaction_ui.review_rows',return_value=False):d.finish('temporary')
    if wr.dumps(win.work_repository.stores())!=persisted or win.config_for(s)!=config:raise AssertionError('cancelled parameter review wrote settings')
    shot(d,'interaction-store-editor.png')
    with patch('interaction_ui.confirm',return_value=True):d.reject()
    d.deleteLater()
    d=ParameterDialog(win,config,win.preset_for(s),scope='本店生效参数',readonly=True);d.show();d.tabs.setCurrentIndex(5)
    if not d.tabs.isEnabled() or d.param_table.cellWidget(2,1).isEnabled():raise AssertionError('readonly parameters cannot browse or can edit')
    shot(d,'interaction-effective-readonly.png');d.reject();d.deleteLater()
    d=ParameterDialog(win,config,win.preset_for(s),scope='方案库');d.show()
    if d.retire_btn.isEnabled():raise AssertionError('standard preset can be retired')
    shot(d,'interaction-preset-library.png');QTest.keyClick(d,Qt.Key.Key_Escape);wait(lambda:not d.isVisible());d.deleteLater()
    checks.append('actual widgets: filtered selection vs full batch, hidden count, separate preset manager, read-only effective tabs, cancelled parameter review leaves settings unchanged; Escape closes read-only manager')
    # Confirm-only archive selector does not silently use a highlighted result row.
    def cancel_selector():
        modal=QApplication.activeModalWidget()
        if modal and modal.objectName()=='storeScopeSelection':shot(modal,'interaction-archive-selection.png');modal.reject()
    QTimer.singleShot(40,cancel_selector)
    if choose_stores(win,'选择归档门店',{'QA_A':{'name':'A','state':'已完成'},'QA_B':{'name':'B','state':'待重算'}},['QA_A']):raise AssertionError('cancel selector returned stores')
    # Import replacement cancel does not replace the catalog or priority list.
    before=copy.deepcopy(win.catalogs);before_focus=copy.deepcopy(win.new_focus)
    with patch('interaction_ui.confirm',return_value=False):win.catalog_imported(copy.deepcopy(win.catalogs['河北仓']))
    if win.catalogs!=before:raise AssertionError('cancelled catalog update changed live catalog')
    with patch('application_window.confirm_focus',return_value=False):win.empty_focus()
    if win.new_focus!=before_focus:raise AssertionError('cancelled empty focus changed list')
    checks.append('actual callbacks: archive scope cancellation, warehouse replacement cancellation and empty-focus cancellation preserve data')
    win.show_section(2);wait(lambda:win.history_reader.active is None and bool(win.archive_all))
    head=next(h for h in win.archive_all if h['store']=='QA_A' and h['warehouse']=='河北仓')
    original=wr.dumps(win.orders.get(head['day'],head['store'])['payload'])
    d=ArchiveDialog(win,head);d.show();wait(lambda:d.selected is not None)
    audit=AuditDialog(d,win,d.selected,products=d.model.rows,sku=d.model.rows[0][0]);audit.show();audit.scope.setCurrentText('当时商品资料快照');wait(lambda:audit.loaded_key is not None)
    if audit.export_btn.isEnabled() or audit.export_btn.text()!='导出此核对表单（非原表）' or audit.original_btn.text()!='下载商品资料原表':raise AssertionError('original and derived form share a mutable action')
    shot(audit,'interaction-independent-original.png');audit.reject();audit.deleteLater();d.reject();d.deleteLater()
    bad=next(p for p in (out/'exports').glob('*.xlsx') if '山东仓' in p.name)
    with closing(win.work_repository.connect()) as db:before_reply=db.execute('SELECT * FROM merchant_feedback').fetchall()
    errors=[]
    with patch('import_ui.choose',return_value=[str(bad)]),patch('feedback_ui.QMessageBox.warning',side_effect=lambda *args:errors.append(args[2])):import_returns(win,win,head)
    with closing(win.work_repository.connect()) as db:after_reply=db.execute('SELECT * FROM merchant_feedback').fetchall()
    if not errors or '其它单据' not in errors[0] or after_reply!=before_reply:raise AssertionError('wrong warehouse reply was not rejected without writing')
    if wr.dumps(win.orders.get(head['day'],head['store'])['payload'])!=original:raise AssertionError('interaction audit changed archive')
    data=events(win.work_repository.path,head['oid'],'河北仓');d=EventsDialog(win,data['records'],data['rows']);d.show();shot(d,'interaction-events.png')
    if any(b.text()=='导出完整计算表' for b in d.findChildren(QPushButton)):raise AssertionError('event page still masquerades order exports as log exports')
    d.reject();d.deleteLater()
    checks.append('actual frozen reply import rejects another warehouse before commit; immutable signed snapshot; independent original/form controls; event page directs to exact related order')
