"""Release verification against previous accepted outputs; never edits sources."""
import sys,json,copy,threading,hashlib
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent/'src'))
import engine,openpyxl
root=Path(__file__).parent
qa=root/'qa/v02';qa.mkdir(parents=True,exist_ok=True)
source=Path(r'D:\企业微信\WXWork\1688855943717538\Cache\File\2026-10')
reference=Path(r'D:\优团工作文件夹\outputs\luquan_model_comparison_20261029\原模型副本_仅一箱例外\首次验算')
original=Path(r'D:\优团工作文件夹\outputs\python_replenishment_20261022')
files=[source/'补货建议导出服务.xlsx',source/'混合仓商品资料.xlsx',original/'model.py',original/'run.py',original/'config.json']
digest=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
before={str(p):digest(p) for p in files}
assert digest(original/'model.py')==digest(root/'src/model.py')
assert digest(original/'run.py')==digest(root/'src/input_adapter.py')
loaded=engine.load_inputs(*files[:2],progress=print)
store=next(iter(loaded['stores']))
off=engine.run_calculation(loaded,store,engine.DEFAULT_WAREHOUSES,False)
on=engine.run_calculation(loaded,store,engine.DEFAULT_WAREHOUSES,True)
print(on['summary'],flush=True)
for name,result in [('baseline',off),('trial',on)]:
    expected=[json.loads(s) for s in (reference/(name+'.jsonl')).read_text(encoding='utf-8').splitlines()]
    assert len(expected)==len(result['rows'])==14244
    diffs=[]
    for a,b in zip(expected,result['rows']):
        assert (a['B'],a['E'],a['_meta']['export_row'])==(b['B'],b['E'],b['_meta']['export_row'])
        if (a['AS'],a['BV'])!=(b['AS'],b['BV']):diffs.append([a['E'],a['AS'],b['AS'],a['BV'],b['BV']])
    assert not diffs,(name,diffs[:10],len(diffs))
changes=[b for a,b in zip(off['rows'],on['rows']) if (a['AS'],a['BV'])!=(b['AS'],b['BV'])]
assert len(changes)==8
assert on['summary']['local']==5161 and on['summary']['extra']==378.8
assert on['summary']['calculated_amount']==23833.55 and off['summary']['calculated_amount']==23454.75
assert on['summary']['amount']==12268.87 and off['summary']['amount']==11890.07
assert on['summary']['positive']==430 and on['summary']['excluded']==3269
assert on['summary']['excluded_amount']==11564.68 and on['summary']['excluded_positive']==616
for d in on['rows']:
    excluded=d.get('S')=='禁用' or d['_meta'].get('export_status')=='停采'
    if excluded:
        assert engine.order_quantity(d)==0 and engine.order_amount(d)==0 and not engine.orderable(d)
    else:
        assert engine.order_quantity(d)==engine.quantity(d) and engine.order_amount(d)==engine.amount_value(d)
# Independent statuses, OR overlap, and read-only computation values.
seed=copy.deepcopy(changes[0])
for supply,status,expected in [('启用','正常',False),('禁用','正常',True),('启用','停采',True),('禁用','停采',True)]:
    d=copy.deepcopy(seed);d['S']=supply;d['_meta']['export_status']=status;saved=copy.deepcopy(d)
    assert bool(engine.exclusion_reason(d))==expected
    assert engine.order_quantity(d)==(0 if expected else d['AS'])
    assert d==saved
assert all(engine.quantity(d)=='未计算' and engine.amount_value(d)=='未计价' for d in on['rows'] if d['_source']=='地采商品')
assert engine.category_of({}, {'match':'商品资料重复','blocking':['商品编码重复']})=='匹配待核'
assert engine.category_of({}, {'match':'资料未匹配','blocking':['待补商品资料','商品编码为空']})=='匹配待核'
assert engine.category_of({}, {'match':'资料未匹配','blocking':['待补商品资料','库存异常']})=='地采商品'
assert not on['checks']['failures']
# Cancellation, missing inputs and refusing overwrite.
event=threading.Event();event.set()
try:engine.run_calculation(loaded,store,engine.DEFAULT_WAREHOUSES,True,cancel=event)
except engine.Cancelled:pass
else:raise AssertionError('cancel did not stop')
try:engine.load_inputs(files[0],files[0])
except ValueError:pass
else:raise AssertionError('same input accepted')
for path in [files[0],files[1]]:
    try:engine.export_xlsx(on,path)
    except ValueError:pass
    else:raise AssertionError('overwrite accepted')
export=qa/'完整结果.xlsx'
engine.export_xlsx(on,export,progress=print)
book=openpyxl.load_workbook(export,read_only=True,data_only=False)
counts={name:sum(1 for _ in book[name].iter_rows())-1 for name in book.sheetnames}
assert counts['全部明细']==14244 and counts['地采商品']==5161 and counts['一箱例外']==8
assert counts['补货清单']==430 and counts['已排除']==3269
for sheet in ['补货清单','已排除']:
    it=book[sheet].iter_rows(values_only=True);headers=next(it)
    for values in it:
        d=dict(zip(headers,values))
        if sheet=='补货清单':assert d['商品资料状态']!='禁用' and d['补货建议状态']!='停采' and d['执行补货']>0
        else:assert d['执行补货']==d['执行金额']==0 and d['执行状态']=='已排除'
for row in book['全部明细'].iter_rows(min_row=2):
    assert row[1].data_type=='s'
    assert all(c.data_type!='f' for c in row)
book.close()
filtered=qa/'仅一箱例外.xlsx';engine.export_xlsx(on,filtered,changes,'当前筛选：一箱例外')
book=openpyxl.load_workbook(filtered,read_only=True)
assert sum(1 for _ in book['筛选明细'].iter_rows())==9
book.close()
after={str(p):digest(p) for p in files};assert before==after
report={'passed':True,'version':'0.2.0','rows':14244,'unchanged':14236,'changed':8,'filter_excluded':3269,'nonexcluded_values_unchanged':10975,'summary':on['summary'],'baseline_summary':off['summary'],'export_counts':counts,'source_hashes_unchanged':before,'core_exact_copy':True,'reference_comparison':'Both baseline and trial AS/BV identical for every row; only execution filter added','checks':on['checks']}
(qa/'backend_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print('BACKEND PASS',flush=True)
