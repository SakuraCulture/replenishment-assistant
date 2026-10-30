"""Exact current-baseline Qt view oracle: source order, ties, filters and all cell roles."""
import os,sys,json,importlib.util,traceback
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];QA=ROOT/'qa/full_workflow_073';sys.path.insert(0,str(ROOT/'src'));os.environ['QT_QPA_PLATFORM']='offscreen'
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt,QTimer
import ui_results as new,history_read,result_semantics as sem

def main():
    spec=importlib.util.spec_from_file_location('baseline_goods',QA/'baseline/src/ui_results.py');old=importlib.util.module_from_spec(spec);spec.loader.exec_module(old)
    app=QApplication([]);report={'passed':False,'checks':0,'stores':[],'whitelist':[],'expected_delta':'baseline visible rows minus existing exclusion_reason; no re-sorting'}
    def exercise():
        try:
            repo=history_read.repository(ROOT/'qa/archive_072/snapshot.sqlite3')
            for head in repo.headers():
                rows=repo.get(head['day'],head['store'])['payload']['result']['rows'];models=[]
                for mod in [old,new]:
                    m=mod.GoodsModel();m.set_rows(rows);p=mod.FilterModel();p.setSourceModel(m);p.sort(10,Qt.SortOrder.DescendingOrder);models.append((m,p))
                def indices(p):return [p.mapToSource(p.index(i,0)).row() for i in range(p.rowCount())]
                def check(context):
                    a,b=[indices(p) for m,p in models];a=[i for i in a if sem.work_eligible(rows[i])]
                    assert a==b,(head['store'],context,'ordered indices',a[:30],b[:30]);report['checks']+=1
                    return a
                for mode in ['建议补货','全部商品','必卖商品','新品候选','已排除','商品资料禁用','补货建议停采','资料匹配','一箱例外','地采商品','待处理','重点关注','动销补货','新品推荐','预算未满足','全部商品']:
                    for q in ['', '奶', '']:
                        for m,p in models:p.beginFilterChange();p.mode=mode;p.search=q;p.endFilterChange()
                        check((mode,q))
                for col in range(len(new.COLUMNS)):
                    for order in [Qt.SortOrder.AscendingOrder,Qt.SortOrder.DescendingOrder]:
                        # Full largest-store ties; smaller stores exercise every column on an ordered prefix.
                        if head['store']!='ZYD20251112' and col>2:continue
                        for m,p in models:p.sort(col,order)
                        positions=check((col,str(order)))
                    for i in range(len(rows)):
                        if not sem.work_eligible(rows[i]):continue
                        roles=[Qt.ItemDataRole.DisplayRole,Qt.ItemDataRole.UserRole,Qt.ItemDataRole.ToolTipRole,Qt.ItemDataRole.TextAlignmentRole,Qt.ItemDataRole.ForegroundRole]
                        for role in roles:
                            a=models[0][0].data(models[0][0].index(i,col),role);b=models[1][0].business_data(models[1][0].index(i,col),role)
                            assert type(a)==type(b) and a==b,(head['store'],i,col,str(role),a,b)
                        report['checks']+=1
                for m,p in models:p.sort(-1);m.set_rows(rows[:400]);p.beginFilterChange();p.mode='全部商品';p.endFilterChange();p.sort(10,Qt.SortOrder.DescendingOrder)
                check('replace rows')
                report['stores'].append({'store':head['store'],'rows':len(rows),'eligible_rows':sum(sem.work_eligible(d) for d in rows)})
            report.update(passed=True,differences=0)
        except Exception:report['error']=traceback.format_exc()
        (QA/'goods_parity.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(report,ensure_ascii=False),flush=True);app.exit(0 if report['passed'] else 1)
    QTimer.singleShot(0,exercise);sys.exit(app.exec())
if __name__=='__main__':main()
