"""Opt-in acceptance for 0.9.2 additions, called only by business smoke mode."""
import copy
from PySide6.QtCore import Qt,QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QDialog,QVBoxLayout,QTableWidget,QComboBox,QPushButton,QMessageBox,QCheckBox,QApplication
from parameter_ui import ParameterDialog
from conflict_ui import add_bulk_choices
from ui_tasks import BatchWorker

def verify(win,out):
    checks=[]
    # Real keyboard events and enabled-button semantics; no user data selected.
    win.activateWindow();win.raise_();QTest.qWait(60)
    for key,expected in [(Qt.Key.Key_1,0),(Qt.Key.Key_2,1),(Qt.Key.Key_3,2)]:
        QTest.keyClick(win,key,Qt.KeyboardModifier.ControlModifier);QTest.qWait(40);assert win.sections.currentIndex()==expected
    checks.append('actual Ctrl+1/2/3 navigation shortcuts')
    preset=win.preset_for('__qa__');current=copy.deepcopy(preset['config']);current['parameters']['cap_days']=20
    pd=ParameterDialog(win,current,preset,scope='所选 1 家门店');pd.show();i=list(current['parameters']).index('cap_days')
    # HELP order, not arbitrary config ordering.
    import parameter_schema
    i=list(parameter_schema.HELP).index('cap_days');pd.param_table.cellWidget(i,1).setValue(19);pd.tabs.setCurrentWidget(pd.effective)
    row=next(i for i in range(pd.effective.rowCount()) if pd.effective.item(i,0).text()=='parameters / cap_days')
    assert [pd.effective.item(row,j).text() for j in (1,2,3,4)]==['21','21','20','19.0'];assert pd.effective.item(row,5).text()=='本次编辑'
    assert pd.grab().save(str(out/'08-effective-parameters.png'));pd.reject();pd.deleteLater();checks.append('actual parameter effective comparison, cancel does not apply')
    d=QDialog(win);d.setWindowTitle('QA 批量冲突');v=QVBoxLayout(d);table=QTableWidget(3,3);v.addWidget(table);cs=[{'store':'s','sku':str(i),'variants':[{'path':'A'},{'path':'B'}]} for i in range(3)];combos=[]
    from PySide6.QtWidgets import QTableWidgetItem
    for i,c in enumerate(cs):
        table.setItem(i,1,QTableWidgetItem('A B'));combo=QComboBox();combo.addItem('暂不处理',None);combo.addItem('A',0);combo.addItem('B',1);combos.append(combo);table.setCellWidget(i,2,combo)
    combos[2].setCurrentIndex(2);add_bulk_choices(d,v,table,cs,combos);d.show()
    action=next(b for b in d.findChildren(QPushButton) if b.text()=='按来源批量提出选择');defaults=[]
    def answer(yes):
        box=QApplication.activeModalWidget();defaults.append(box.defaultButton()==box.button(QMessageBox.StandardButton.No));box.done(QMessageBox.StandardButton.Yes if yes else QMessageBox.StandardButton.No)
    QTimer.singleShot(50,lambda:answer(False));action.click();assert [c.currentData() for c in combos]==[None,None,1]
    QTimer.singleShot(50,lambda:answer(True));action.click();assert [c.currentData() for c in combos]==[0,0,1];assert all(defaults)
    next(b for b in d.findChildren(QPushButton) if b.text()=='撤销批量选择').click();assert [c.currentData() for c in combos]==[None,None,1];d.reject();d.deleteLater();checks.append('bulk source proposal default cancel, confirmation, preserves prior choice, undo')
    saved=(win.worker,win.states,win.run_codes);fake=BatchWorker({}, {},None,1,win)
    try:
        win.worker=fake;win.states={'qa':{'name':'测试店','state':'计算中','error':''}};win.run_codes=['qa'];win.cancel_work();win.refresh_tables()
        assert fake.cancel.is_set() and '等待正在运行的 1 家' in win.run_note.text() and not win.stop_btn.isEnabled() and not win.cancel_btn.isEnabled()
    finally:win.worker,win.states,win.run_codes=saved;fake.deleteLater();win.refresh_tables();win.update_controls()
    checks.append('stop request remains visible through refresh, buttons disabled; fake unstarted worker for UI state only')
    import allocation,normal_exports
    common=dict(K=0,L=0,H=1,I=7,J=30,AN=1,AM=1,AH=5,BD=1,_source='资料匹配',_meta={'blocking':[],'export_status':'正常'},S='启用')
    rows=[dict(common,E='base',V=10,_candidate_qty=10,_candidate_amount=100,_must_sell=True),dict(common,E='last',V=5,_candidate_qty=1,_candidate_amount=5,_must_sell=False)]
    receipt=allocation.apply(rows,100);assert receipt['spent']==105 and receipt['overrun']['sku']=='last' and normal_exports.decision(rows[1])=='预算补足'
    rows=[dict(common,E='zero',V=5,_candidate_qty=1,_candidate_amount=5,_must_sell=False)];assert allocation.apply(rows,0)['spent']==0
    checks.append('packaged budget implementation: original100 target + one5 unit =105, merchant reason, zero budget stays zero (labelled synthetic input)')
    return checks
