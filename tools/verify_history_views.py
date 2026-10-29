"""Exact table roles/order/filter/summary and paginated-log export regression."""
import os,sys,json,importlib.util,time
from pathlib import Path
os.environ['QT_QPA_PLATFORM']='offscreen';ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt
import ui_results as new,history_read as history,work_repository as wr

def main():
    qa=ROOT/'qa/archive_072';spec=importlib.util.spec_from_file_location('baseline_results',qa/'baseline/src/ui_results.py');old=importlib.util.module_from_spec(spec);spec.loader.exec_module(old)
    app=QApplication([]);repo=history.repository(qa/'snapshot.sqlite3');head=repo.headers()[-1];result=repo.get(head['day'],head['store'])['payload']['result'];rows=result['rows'];checks=0
    models=[]
    for mod in [old,new]:
        model=mod.GoodsModel();model.set_rows(rows);proxy=mod.FilterModel();proxy.setSourceModel(model);proxy.sort(10,Qt.SortOrder.DescendingOrder);models.append((model,proxy))
    def indices(proxy):return [proxy.mapToSource(proxy.index(i,0)).row() for i in range(proxy.rowCount())]
    for mode in ['全部商品','建议补货','必卖商品','预算未满足','最低持有缺口','已排除','商品资料禁用','补货建议停采','资料匹配','一箱例外','地采商品','待处理','重点关注','动销补货','新品候选','新品推荐','缺货恢复及其他']:
        values=[]
        for model,proxy in models:proxy.mode=mode;proxy.invalidateFilter();values.append(indices(proxy))
        assert values[0]==values[1],mode
        assert wr.dumps(old.semantics.summarize([rows[i] for i in values[0]]))==wr.dumps(new.semantics.summarize([rows[i] for i in values[1]]));checks+=1
    for model,proxy in models:model.set_rows(rows[:400]);proxy.mode='全部商品';proxy.invalidateFilter()
    for col in range(len(new.COLUMNS)):
        for order in [Qt.SortOrder.AscendingOrder,Qt.SortOrder.DescendingOrder]:
            for _,proxy in models:proxy.sort(col,order)
            assert indices(models[0][1])==indices(models[1][1]),(col,order);checks+=1
        for i in [0,1,10,399]:
            for role in [Qt.ItemDataRole.DisplayRole,Qt.ItemDataRole.UserRole,Qt.ItemDataRole.ToolTipRole,Qt.ItemDataRole.TextAlignmentRole,Qt.ItemDataRole.ForegroundRole]:
                a=models[0][0].data(models[0][0].index(i,col),role);b=models[1][0].data(models[1][0].index(i,col),role);assert type(a)==type(b) and a==b,(i,col,role,a,b)
    from history_ui import EventsDialog
    records=repo.events();d=EventsDialog(None,records*30)
    for q in ['', '确认','%','0', '无匹配']:
        d.search.setText(q);d.filter();expected=[r for r in records*30 if q.lower() in wr.dumps(r).lower()];assert d.matches==expected
        assert len(d.matches)>100 or q!='';d.turn(1);assert d.matches==expected;checks+=1
    report={'passed':True,'differences':0,'checks':checks,'real_rows':len(rows),'order':'original Qt order including ties, default full-store sort + all columns both directions on 400 rows','roles':'Display/User/Tooltip/Alignment/Foreground including type','event_export_scope':'full matching fixed snapshot independent of displayed page','whitelist':[]}
    (qa/'stage_c.json').write_text(json.dumps(report,indent=2));print(json.dumps(report))
if __name__=='__main__':main()
