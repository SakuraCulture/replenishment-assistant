"""Real five-store virtual first-fill parity; expected exclusion delta is explicit."""
import os,sys,json,importlib.util,time,traceback
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];QA=ROOT/'qa/full_workflow_073'
sys.path.insert(0,str(ROOT/'src'));os.environ['QT_QPA_PLATFORM']='offscreen'
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt,QTimer,QCoreApplication,QEvent
import new_product_ui as new,work_repository as wr,history_read,result_semantics as sem,new_product_focus as focus

def module(name,path):
    spec=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m

def main():
    old=module('baseline_new_product',QA/'baseline/src/new_product_ui.py');app=QApplication([]);report={'passed':False,'checks':0,'stores':[],'whitelist':[],'intentional_delta':'Remove only exclusion_reason rows from working and exported product lists; retain raw calculation/archive.'}
    def exercise():
        try:
            repo=history_read.repository(ROOT/'qa/archive_072/snapshot.sqlite3')
            for head in repo.headers():
                result=repo.get(head['day'],head['store'])['payload']['result']
                a=old.NewProductDialog(None,result,result,focus.empty('2026-10'));b=new.NewProductDialog(None,result,result,focus.empty('2026-10'))
                allowed=[i for i,d in enumerate(a.rows) if sem.work_eligible(d)];assert [a.rows[i]['E'] for i in allowed]==[d['E'] for d in b.rows]
                def visible(dialog,is_new):return [dialog.rows[i]['E'] for i in (dialog.model.visible if is_new else [i for i in allowed if not dialog.table.isRowHidden(i)])]
                for scope in ['全部新品','可选','本月新品','往期新品','必卖','已选','未获预算']:
                    for q in ['', '奶', '0', 'QA-NO-MATCH']:
                        for dialog in [a,b]:dialog.scope.setCurrentText(scope);dialog.search.setText(q);dialog.refresh()
                        assert visible(a,False)==visible(b,True),(head['store'],scope,q);report['checks']+=1
                for dialog in [a,b]:dialog.scope.setCurrentText('全部新品');dialog.search.clear();dialog.refresh()
                roles=[Qt.ItemDataRole.DisplayRole,Qt.ItemDataRole.ToolTipRole,Qt.ItemDataRole.UserRole,Qt.ItemDataRole.CheckStateRole]
                for i,original in enumerate(allowed):
                    for col in range(11):
                        ix=b.model.index(i,col);item=a.table.item(original,col)
                        assert item.flags()==b.model.flags(ix),(i,col,'flags',item.flags(),b.model.flags(ix))
                        for role in roles:
                            x=item.data(role);y=b.model.data(ix,role)
                            # Qt exposes check-state enum as integer from item; semantic enum value identical.
                            if role==Qt.ItemDataRole.CheckStateRole and y is not None:y=y.value
                            assert type(x)==type(y) and x==y,(head['store'],i,col,str(role),x,y,type(x),type(y))
                        report['checks']+=1
                valid=next((i for i in range(len(b.rows)) if b.widgets[i][0].isEnabled()),None)
                if valid is not None:
                    source=allowed[valid]
                    for dialog,index in [(a,source),(b,valid)]:
                        dialog.widgets[index][1].setValue(2*dialog.rows[index]['BD']);dialog.widgets[index][0].setChecked(True);dialog.search.setText('QA-NO-MATCH');dialog.refresh()
                    assert a.selections()==b.selections();assert a.note.text()==b.note.text();report['checks']+=1
                report['stores'].append({'store':head['store'],'rows':len(result['rows']),'old_new_candidates':len(a.rows),'working_candidates':len(b.rows),'removed_disabled_stopped':len(a.rows)-len(b.rows)})
                a.close();b.close();a.deleteLater();b.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
            report['passed']=True;report['differences']=0
        except Exception:report['error']=traceback.format_exc()
        (QA/'new_product_parity.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(report,ensure_ascii=False),flush=True);app.exit(0 if report['passed'] else 1)
    QTimer.singleShot(0,exercise);sys.exit(app.exec())
if __name__=='__main__':main()
