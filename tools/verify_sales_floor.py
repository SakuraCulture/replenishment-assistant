"""Compare frozen old rules with current real store entry; synthetic data only."""
import argparse,ast,copy,csv,hashlib,json,math,subprocess,sys,types
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]
import engine,model,input_adapter as adapter,normal_exports as ne,result_semantics as sem
from test_sales_floor import calculate

CASES=[
 ('A_双零有销量',{'L':0},{}),('A2_双零保底必需',{'H':.07,'I':1,'J':1,'L':0},{}),
 ('B_在途40资格不通过',{}, {'moq':0}),('B2_资格不通过开例外',{}, {'moq':0,'exception':True}),
 ('C_在途40正常缺口',{},{}),('D1_在途覆盖目标',{'L':80},{'moq':100}),('D2_现货覆盖目标',{'K':80,'L':0},{'moq':100}),
 ('E_有现货',{'K':1,'L':0},{}),('E2_无销量无缺货',{'H':0,'I':0,'J':0,'L':0},{}),
 ('E3_零销短期恢复',{'H':0,'I':0,'J':0,'L':0,'N':4},{}),('E4_极端恢复',{'H':0,'I':0,'J':0,'L':0,'N':30},{}),
 ('E5_负可用抵消正在途',{'K':-40,'L':40},{}),('E6_负可用无在途',{'K':-5,'L':0},{}),
 ('E7_缺失可用',{'K':None},{}),('E8_缺失在途',{'L':None},{}),('E9_在途非法',{'L':'未知'},{}),
 ('E10_负在途',{'K':0,'L':-1},{}),('E11_微量在途',{'L':1e-10},{}),
 ('F1_小在途资金约束',{'H':.07,'I':1,'J':1,'L':.5},{'cap':0}),
 ('F2_独立一箱例外',{'H':.07,'I':1,'J':1,'L':.5},{'cap':0,'exception':True}),
 ('F3_必采最低持有',{'H':.07,'I':1,'J':1,'L':.5},{'must':True}),('F4_必采在途已覆盖',{}, {'moq':0,'must':True}),
 ('F5_预算为0',{}, {'budget':0}),('F6_预算充足',{}, {'budget':100}),
 ('F7_禁用',{}, {'status':'禁用'}),('F8_日配',{}, {'tags':'日配'}),('F9_停采',{}, {'export_status':'停采'}),('F10_禁采',{}, {'export_status':'禁采'}),
 ('F11_小在途零销恢复',{'H':0,'I':0,'J':0,'L':.5,'N':4},{'cap':0}),('F12_小在途极端恢复',{'H':0,'I':0,'J':0,'L':.5,'N':30},{'cap':0})]

def git(*args):return subprocess.check_output(['git',*args],cwd=ROOT)
def sanitized(v):
    if isinstance(v,dict):return {k:sanitized(x) for k,x in v.items()}
    if isinstance(v,(list,tuple)):return [sanitized(x) for x in v]
    if isinstance(v,float) and not math.isfinite(v):return str(v)
    return v
def main():
    p=argparse.ArgumentParser();p.add_argument('--baseline',required=True);p.add_argument('--out',required=True,type=Path);a=p.parse_args()
    out=a.out.resolve();out.relative_to(ROOT/'qa');out.mkdir(parents=True,exist_ok=False)
    commit=git('rev-parse','--verify',a.baseline+'^{commit}').decode().strip()
    old_source=git('show',commit+':src/model.py').decode('utf-8');old=types.ModuleType('sales_floor_old_baseline');sys.modules[old.__name__]=old
    exec(compile(old_source,'frozen-baseline:model.py','exec'),old.__dict__)
    source=git('show',commit+':src/input_adapter.py').decode('utf-8');node=next(n for n in ast.parse(source).body if isinstance(n,ast.FunctionDef) and n.name=='business_checks')
    namespace=dict(adapter.__dict__);exec(compile(ast.get_source_segment(source,node),'frozen-baseline:business_checks','exec'),namespace)
    unchanged={}
    for name in ('engine.py','one_box_exception.py','mandatory_holding.py','allocation.py','default_config.json','default_must_sell.json','new_products.py','warehouse_routing.py'):
        current=(ROOT/'src'/name).read_bytes();assert current==git('show',commit+':src/'+name),name
        unchanged[name]=hashlib.sha256(current).hexdigest()
    results=[]
    fields='K L P AN AM AP AL AQ BI BJ BK AR BP BQ BR BU AS BV BM AW'.split()
    for name,changes,options in CASES:
        records={}
        for variant in ('before','after'):
            if variant=='before':
                with patch.object(adapter,'calculate',old.calculate),patch.object(adapter,'business_checks',namespace['business_checks']):r,d=calculate(changes,**options)
            else:r,d=calculate(changes,**options)
            records[variant]={k:d.get(k) for k in fields}|{'ordinary':d['AR'] if d.get('BI')==1 else 0,'execution':sem.order_quantity(d),
                'short_reason':ne.decision(d),'export_reason':sem.explanation(d),'exception':copy.deepcopy(d['_audit']),
                'mandatory':copy.deepcopy(d.get('_mandatory_holding')),'checks':r['checks'],'weights':r['config']['weight_profiles']['快销'],
                'parameters':r['config']['parameters'],'warehouse_days':r['warehouses'],'floor_rule':d.get('_floor_rule')}
        assert all(records['before'][k]==records['after'][k] for k in ('AN','AM','AP','AL','AQ','BI','BJ','BK','AR')),name
        results.append({'name':name,'input_changes':changes,'options':options,**records})
    lookup={r['name']:r for r in results}
    assert (lookup['B_在途40资格不通过']['before']['execution'],lookup['B_在途40资格不通过']['after']['execution'])==(20,0)
    assert lookup['C_在途40正常缺口']['after']['execution']==20
    assert lookup['A2_双零保底必需']['after']['execution']==20
    assert lookup['F1_小在途资金约束']['after']['execution']==0
    assert lookup['F2_独立一箱例外']['after']['short_reason']=='一箱恢复'
    assert lookup['F3_必采最低持有']['after']['short_reason']=='必采最低持有'
    report={'baseline':commit,'source_mode':'current uncommitted workspace','synthetic':True,'original_order_parameters_known':False,'live_platform':False,
        'forecast_net_qualification_and_cap_unchanged':True,'unchanged_sources':unchanged,'cases':results}
    (out/'comparison.json').write_text(json.dumps(sanitized(report),ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8',newline='\n')
    with (out/'comparison.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.writer(f);writer.writerow(['场景','版本','可用','在途','净缺口AQ','普通资格BI','普通候选AR','BP','BQ最低恢复','模型AS','最终执行','导出短理由','导出长理由','参数','分类权重','仓周期'])
        for r in results:
            for variant in ('before','after'):
                d=r[variant];writer.writerow([r['name'],variant,d['K'],d['L'],d['AQ'],d['BI'],d['AR'],d['BP'],d['BQ'],d['AS'],d['execution'],d['short_reason'],d['export_reason'],json.dumps(d['parameters'],ensure_ascii=False),json.dumps(d['weights'],ensure_ascii=False),json.dumps(d['warehouse_days'],ensure_ascii=False)])
    print(json.dumps({'cases':len(results),'passed':True,'out':str(out),'live_platform':False},ensure_ascii=False))
if __name__=='__main__':main()
