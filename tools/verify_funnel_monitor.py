"""Isolated baseline comparison, example and timing for template-style export."""
import argparse
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import statistics
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]
import openpyxl
import test_order_analytics as fixtures
import order_analytics_exports as current


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def check(a,b,label):
    if a!=b:raise AssertionError(label+': '+repr(a)+' != '+repr(b))


def signature(cell):
    return (cell.value,cell.data_type,cell.number_format,str(cell.font),str(cell.fill),
            str(cell.alignment),str(cell.border),str(cell.protection))


def main():
    p=argparse.ArgumentParser();p.add_argument('--baseline',default='8078308394f527ffee27270eb61dbaf6980472be')
    p.add_argument('--output',type=Path,required=True);p.add_argument('--template',type=Path)
    args=p.parse_args();out=args.output.resolve();out.mkdir(parents=True,exist_ok=False)
    protected=['src/model.py','src/allocation.py','src/one_box_exception.py','src/order_analytics.py','src/engine.py','src/default_config.json']
    hashes={name:sha(ROOT/name) for name in protected}
    for name,value in hashes.items():
        old=subprocess.check_output(['git','show',args.baseline+':'+name],cwd=ROOT)
        check(hashlib.sha256(old).hexdigest(),value,'Protected source '+name)
    template_hash=sha(args.template) if args.template else None
    old_path=out/'baseline_export.py';old_path.write_bytes(subprocess.check_output(['git','show',args.baseline+':src/order_analytics_exports.py'],cwd=ROOT))
    spec=importlib.util.spec_from_file_location('baseline_export',old_path);old=importlib.util.module_from_spec(spec);spec.loader.exec_module(old)
    f=fixtures.AnalyticsTests();f.setUp()
    try:
        f.p['result']['store_name']='合成测试门店（非真实业务）'
        for row in f.p['result']['rows']:row['G']='包'
        f.save_payload()
        f.f.save('return',[{'门店确认数量':12}]);f.f.save('payment',[{'付款单商品数量':6,'实际金额':19.5}]);f.f.save('receipt',[{'实际收货数量':3}])
        report=f.report();before=copy.deepcopy(report);db_hash=sha(f.path)
        timing={'baseline':[],'candidate':[]};receipts={}
        for i in range(3):
            for name,module in [('baseline',old),('candidate',current)] if i%2==0 else [('candidate',current),('baseline',old)]:
                target=out/f'{name}-{i}.xlsx';t=time.perf_counter()
                receipts[name]=module.export(f.path,report,target)
                timing[name].append(time.perf_counter()-t)
        books=[openpyxl.load_workbook(out/f'{name}-2.xlsx') for name in ('baseline','candidate')]
        cells=0
        try:
            a,b=books
            check([s for s in b.sheetnames if s in a.sheetnames],a.sheetnames,'Legacy sheet order')
            for name in a.sheetnames:
                if name=='统计口径':
                    # Exact presentation-note differences; no business fields ignored.
                    aa={r[0].value:r[1].value for r in a[name]};bb={r[0].value:r[1].value for r in b[name]}
                    check(set(bb)-set(aa),{'监控样式版本'},'Added notes')
                    for key in aa:
                        if key not in ('生成时间','数量'):check(aa[key],bb[key],'Note '+key)
                    continue
                check(a[name].max_row,b[name].max_row,name+' rows');check(a[name].max_column,b[name].max_column,name+' cols')
                for x,y in zip(a[name],b[name]):
                    for c,d in zip(x,y):check(signature(c),signature(d),name+'!'+c.coordinate);cells+=1
            # Every new sheet must be safe from formula injection and Excel errors.
            for name in b.sheetnames[:6]:
                for row in b[name]:
                    for c in row:
                        if c.data_type in ('f','e'):raise AssertionError('Unexpected formula/error '+name+'!'+c.coordinate)
        finally:
            for b in books:b.close()
        check(before,report,'Frozen report mutated');check(db_hash,sha(f.path),'Database mutated')
        current.export(f.path,report,out/'门店补货订单漏斗_合成样例.xlsx')
        # Selected-order scope must apply to the new overview as well as old detail.
        selected=[report['heads'][0]['number']]
        current.export(f.path,report,out/'selected.xlsx',orders=selected)
        b=openpyxl.load_workbook(out/'selected.xlsx')
        try:
            check(next(r[1].value for r in b['漏斗汇总'] if r[0].value=='补货订单数（店 × 仓 × 轮次）'),1,'Selected overview')
            check({r[4].value for r in list(b['门店货值记录'])[4:]},set(selected),'Selected rows')
        finally:b.close()
        if args.template:check(template_hash,sha(args.template),'Original template changed')
        evidence={'passed':True,'baseline':args.baseline,'synthetic':True,'old_sheet_cells_compared':cells,'business_cell_differences':0,
                  'allowed_presentation_changes':['新增6张监控表','统计口径新增监控样式版本','统计口径数量说明','导出类型中文名称'],
                  'dynamic_exclusions':['统计口径/生成时间'],'protected_source_sha256':hashes,
                  'database_unchanged':True,'report_unchanged':True,'template_sha256':template_hash,
                  'performance':{k:{'runs_seconds':v,'median_seconds':statistics.median(v),'min_seconds':min(v),'max_seconds':max(v)} for k,v in timing.items()},
                  'performance_limit':'同机同依赖、合成单店双仓2商品、温缓存、AB/BA/AB逐个运行；包含完整校验及Excel保存；不能代表真实大批量性能。新增版多6张监控表，不宣称提速。',
                  'example':'门店补货订单漏斗_合成样例.xlsx'}
        (out/'report.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2),'utf-8')
        print(json.dumps(evidence,ensure_ascii=False,indent=2))
    finally:f.tearDown()

if __name__=='__main__':main()
