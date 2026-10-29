import copy,csv,hashlib,json,sys,threading
from decimal import Decimal
from pathlib import Path
root=Path(__file__).parent;sys.path.insert(0,str(root/'src'))
import engine,priority_list,allocation,openpyxl
qa=root/'qa/v03';qa.mkdir(parents=True,exist_ok=True)
source=Path(r'D:\企业微信\WXWork\1688855943717538\Cache\File\2026-10')
list_path=source/'必卖清单冬季清单经营分析_0926-1026.xlsx'
original=Path(r'D:\优团工作文件夹\outputs\python_replenishment_20261022')
paths=[source/'补货建议导出服务.xlsx',source/'混合仓商品资料.xlsx',list_path,original/'model.py',original/'run.py',original/'config.json']
digest=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
hashes={str(p):digest(p) for p in paths}
profile=priority_list.import_profile(list_path)
assert len(profile['items'])==2142 and profile['ignored_rows']==199 and profile['duplicate_rows']==0
assert profile['revision']==priority_list.Repository(qa/'config').profile()['revision']
repo=priority_list.Repository(qa/'config');repo.save(profile)
repo2=priority_list.Repository(qa/'config');assert repo2.profile()==profile
repo.save_budget('A',True,3000);repo.save_budget('B',False,0)
assert repo2.budgets()['A']=={'enabled':True,'amount':3000.0}
assert repo2.budgets()['B']['enabled'] is False
backup=qa/'list_backup.json';priority_list.export_profile(profile,backup);assert priority_list.import_profile(backup)==profile
before_profile=digest(qa/'config/must_sell.json')
bad=qa/'bad.csv';bad.write_text('商品编码,商品名称\n123,one\n123,two\n',encoding='utf-8-sig')
try:repo.save(priority_list.import_profile(bad))
except ValueError:pass
else:raise AssertionError('conflicting duplicate accepted')
assert digest(qa/'config/must_sell.json')==before_profile
assert digest(original/'model.py')==digest(root/'src/model.py')
assert digest(original/'run.py')==digest(root/'src/input_adapter.py')
loaded=engine.load_inputs(*paths[:2],progress=print)
base=engine.run_calculation(loaded,'LQD2025',engine.DEFAULT_WAREHOUSES,True,None,profile)
assert base['summary']['amount']==12268.87 and base['summary']['positive']==430
reference=Path(r'D:\优团工作文件夹\outputs\luquan_model_comparison_20261029\原模型副本_仅一箱例外\首次验算\trial.jsonl')
old=[json.loads(s) for s in reference.read_text(encoding='utf-8').splitlines()]
for a,b in zip(old,base['rows']):
    assert a['E']==b['E'] and a['AS']==b['AS'] and a['BV']==b['BV']
    excluded=a.get('S')=='禁用' or a['_meta']['export_status']=='停采'
    assert engine.order_quantity(b)==(0 if excluded else '未计算' if b['_source']=='地采商品' else a['AS'])
scenarios=[];chosen=None
for amount in [0,1000,3000,5000,10000,12268.87,20000]:
    r=engine.apply_budget(copy.deepcopy(base),amount,profile)
    b=r['budget'];assert Decimal(b['spent_exact'])<=Decimal(str(amount))
    assert round(b['spent']+b['remaining'],2)==amount
    assert abs(b['must_spent']+b['other_spent']-b['spent'])<1e-8
    assert r['summary']['amount']==round(b['spent'],2)
    unmet_must=[]
    for old,d in zip(base['rows'],r['rows']):
        assert all(d.get(k)==v for k,v in old.items() if not k.startswith('_'))
        q=engine.order_quantity(d);cap=d['_candidate_qty']
        if engine.exclusion_reason(d):assert q==engine.order_amount(d)==0
        if engine.numeric(q) and engine.numeric(cap):
            assert 0<=q<=cap
            if q>0:assert abs(q/d['BD']-round(q/d['BD']))<1e-7
            if d.get('_must_sell') and d.get('_budget_gap',0)>0 and engine.numeric(d.get('V')) and d['V']>0:unmet_must.append(Decimal(str(d['BD']))*Decimal(str(d['V'])))
        if amount>=12268.87:
            assert q==engine.order_quantity(old)
            if engine.numeric(engine.order_amount(d)):assert abs(engine.order_amount(d)-engine.order_amount(old))<1e-8
    if unmet_must:assert min(unmet_must)>Decimal(str(b['other_spent']))+Decimal(str(b['remaining']))-Decimal('.00000001')
    if amount==0:assert b['spent']==0 and r['summary']['positive']==0
    summary={'budget':amount,**b,'order_skus':r['summary']['positive']}
    scenarios.append(summary);print(json.dumps(summary,ensure_ascii=False),flush=True)
    if amount==3000:chosen=r
# Independent, synthetic budget boundaries: strict list priority, unaffordable carton residual, cents, holding gaps.
def item(sku,must,qty,step,price):
    return {'E':sku,'_must_sell':must,'_candidate_qty':qty,'_candidate_amount':qty*price,'V':price,'BD':step,'H':1,'I':7,'J':30,'K':0,'L':0,'AN':1,'AM':1,'AH':5}
rows=[item('mandatory',True,12,6,1),item('ordinary',False,10,1,1)]
b=allocation.apply(rows,8);assert rows[0]['_allocated_qty']==6 and rows[1]['_allocated_qty']==2 and b['spent']==8
rows=[item('mandatory',True,2,1,5),item('ordinary',False,20,1,1)]
b=allocation.apply(rows,5);assert rows[0]['_allocated_qty']==1 and rows[1]['_allocated_qty']==0 and rows[1]['_holding_gap']
rows=[item('cents',True,9,1,.03)];b=allocation.apply(rows,.10);assert rows[0]['_allocated_qty']==3 and b['spent']==.09
rows=[item('missing-price',True,3,1,0)];b=allocation.apply(rows,100);assert rows[0]['_allocated_qty']==0 and b['unpriced']==1
for bad_budget in [-1,True,'NaN','1.001']:
    try:allocation.apply([],bad_budget)
    except ValueError:pass
    else:raise AssertionError('bad budget accepted')
event=threading.Event();event.set()
try:engine.apply_budget(copy.deepcopy(base),3000,profile,cancel=event)
except engine.Cancelled:pass
else:raise AssertionError('cancel ignored')
path=qa/'预算3000_程序导出.xlsx';engine.export_xlsx(chosen,path)
w=openpyxl.load_workbook(path,read_only=True,data_only=False)
orders=w['补货清单'].iter_rows(values_only=True);heads=next(orders);spent=Decimal(0);count=0
for values in orders:
    row=dict(zip(heads,values));assert row['商品资料状态']!='禁用' and row['补货建议状态']!='停采'
    spent+=Decimal(str(row['执行金额']));count+=1
assert spent<=3000 and count==chosen['summary']['positive']
assert sum(1 for _ in w['预算未满足'].iter_rows())-1==chosen['budget']['deferred']
assert sum(1 for _ in w['全店必卖匹配'].iter_rows())-1==2142
w.close()
assert hashes=={str(p):digest(p) for p in paths}
report={'passed':True,'version':'0.3.0','must_list':base['must_list'],'baseline_summary':base['summary'],'scenarios':scenarios,'source_hashes_unchanged':hashes,
        'checks':['all 14244 AS/BV unchanged','unlimited matches 0.2','strict priority and carton residual','no overspend and no fractional carton','minimum holding gaps visible','price validation','zero and sufficient budgets','persistence and backup roundtrip','bad import preserves saved list','cancellation','native Excel export reread']}
(qa/'backend_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print('BUDGET PASS',flush=True)
