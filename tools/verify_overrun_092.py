"""Independent last-unit oracle applied to fixed pre-change results; exact comparison."""
import copy,json,sys
from decimal import Decimal
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tools')]
from parallel_qa import read,clean,diff,WHITELIST
from structure_oracle import excel_snapshot
import result_semantics as sem,engine,batch_service
Q=ROOT/'qa/verified_092';old=Q/'regression_old';new=Q/'regression_final';changes=[]
D=lambda x:Decimal(str(x))

def expected_result(r):
    r=copy.deepcopy(r);b=r.get('budget',{})
    if r.get('version',{}).get('model_version')=='0.3.2-rules':r['version']['model_version']='0.3.3-rules'
    if not b.get('enabled'):return r
    limit=D(b['limit']);spent=D(b['spent_exact']);remaining=limit-spent;eligible=[]
    for i,d in enumerate(r['rows']):
        if not all(sem.numeric(d.get(k)) for k in ('_candidate_qty','_allocated_qty','BD','V')) or d['V']<=0 or d['BD']<=0:continue
        step=D(d['BD']);cost=step*D(d['V'])
        if D(d['_allocated_qty'])+step>D(d['_candidate_qty'])+D('1e-8') or cost<=remaining or limit<=0 or spent+cost>limit*D('1.05'):continue
        held=D(max(d.get('K',0),0)+max(d.get('L',0),0))+D(d['_allocated_qty']);rate=d.get('AN',0)*d.get('AM',0) if all(sem.numeric(d.get(k)) for k in ('AN','AM')) else 0
        sold=any(sem.numeric(d.get(k)) and d[k]>0 for k in ('H','I','J'))
        urgent=0 if held==0 and sold else 1 if rate>0 and held/D(rate)<D(d.get('AH') or 0) else 2 if rate>0 else 3
        rank=(not bool(d.get('_must_sell')),urgent,held/D(rate) if rate>0 else D('Infinity'),cost,str(d.get('E')),i)
        eligible.append((rank,i,step,cost))
    selected=min(eligible) if eligible else None
    if selected:
        _,i,step,cost=selected;d=r['rows'][i];before=d['_allocated_qty'];qty=D(before)+step;money=qty*D(d['V']);spent+=cost;remaining-=cost
        audit={'quantity':float(step),'unit_cost':float(cost),'overspend':float(-remaining),'budget':float(limit),'ceiling':float(limit*D('1.05')),'rule':'目标预算分配后最多追加一个最小出库单位，超支不超过5%'}
        # Actual allocator writes this audit before replacing execution fields;
        # preserve dictionary insertion order as well as row order.
        bound={}
        for k,v in d.items():
            bound[k]=v
            if k=='_holding_gap':bound['_budget_overrun']=audit
        d=bound;r['rows'][i]=d
        d.update(_allocated_qty=float(qty),_allocated_amount=float(money),_allocated_amount_exact=str(money),_budget_gap=float(D(d['_candidate_qty'])-qty))
        d['_budget_reason']=('已足额分配' if d['_budget_gap']==0 else '预算不足，仅分配部分最小出库单位')+'；末尾追加一个最小出库单位，有限超支'
        d['_holding_gap']=False
        changes.append({'store':r['store'],'budget':float(limit),'sku':d['E'],'name':d.get('D'),'must':d.get('_must_sell'),'old_qty':before,'new_qty':float(qty),'extra_amount':float(cost),'old_spent':b['spent'],'new_spent':float(spent),'overspend':float(-remaining)})
        b.update(spent=float(spent),remaining=float(remaining),spent_exact=str(spent))
        must=sum((D(x.get('_allocated_amount_exact','0')) for x in r['rows'] if x.get('_must_sell')),D(0));b.update(must_spent=float(must),other_spent=float(spent-must),deferred=sum(x.get('_budget_gap',0)>0 for x in r['rows']),holding_gaps=sum(x.get('_holding_gap',False) for x in r['rows']))
        r['summary']=sem.summarize(r['rows'])
    b['overrun']={'ceiling':float(limit*D('1.05')),'amount':float(max(-remaining,D(0))),'sku':str(r['rows'][selected[1]].get('E')) if selected else None,'quantity':float(selected[2]) if selected else 0,'max_units':1,'ratio':.05}
    return r

def project(x):
    if isinstance(x,dict):
        if 'rows' in x and 'summary' in x and 'store' in x:return expected_result(x)
        return {k:project(v) for k,v in x.items()}
    if isinstance(x,(tuple,list)):return type(x)(project(v) for v in x)
    return x

def main():
    names=json.loads((old/'capture.json').read_text())['cases'];assert names==json.loads((new/'capture.json').read_text())['cases'];cases=[];expected={}
    for name in names:
        expected[name]=project(read(old/(name+'.pklz')));cases.append({'case':name,'differences':diff(clean(expected[name]),clean(read(new/(name+'.pklz'))))})
    out=Q/'expected_exports';out.mkdir(exist_ok=False)
    r=expected['real_3000_True'];engine.export_xlsx(r,out/'real.xlsx');engine.export_xlsx(r,out/'filtered.xlsx',[d for d in r['rows'] if d.get('_audit',{}).get('applied')],'当前筛选：一箱例外')
    for name in ('overlap','reversed','warehouse'):
        results={s:r for s,r in expected[name+'_results'].items() if 'rows' in r};batch_service.export_batch(results,{s:{'name':r['store_name'],'state':'已完成','error':''} for s,r in results.items()},out/(name+'.xlsx'))
    for p in out.glob('*.xlsx'):cases.append({'case':p.name,'differences':diff(excel_snapshot(p),excel_snapshot(new/p.name))})
    count=sum(len(c['differences']) for c in cases)
    r={'passed':count==0,'unexpected_differences':count,'cases':cases,'allowed_budget_changes':changes,'whitelist':WHITELIST,'allowed_fields':'target row execution qty/amount/exact/gap/reason/holding and _budget_overrun; derived budget and summary; exact model version0.3.2->0.3.3; no input/intermediate/candidate changes; all remaining order/types exact','oracle':'fixed old results + independently ranked one last unit, strict105% ceiling; no candidate growth; exports rendered from oracle results through otherwise unchanged internal export module'}
    (Q/'overrun_equivalence.json').write_text(json.dumps(r,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps({'passed':not count,'cases':len(cases),'differences':count,'changes':changes},ensure_ascii=False));assert not count
if __name__=='__main__':main()
