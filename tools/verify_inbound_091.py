"""Full frozen signed batch vs fixed baseline core + approved trial oracle."""
import copy,hashlib,importlib.util,json,pickle,sys,time,zlib
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tools')]
import model,one_box_exception as box,result_semantics as sem,work_repository as wr
import input_adapter
from parallel_qa import diff
QA=ROOT/'qa/inbound_091';TRIAL=ROOT.parent/'inbound_floor_trial_20261030'
def module(name,file):
    spec=importlib.util.spec_from_file_location(name,file);m=importlib.util.module_from_spec(spec);sys.modules[name]=m;spec.loader.exec_module(m);return m
old=module('fixed_model_090',QA/'baseline/src/model.py');oldbox=module('fixed_box_090',QA/'baseline/src/one_box_exception.py')
approved=json.loads((TRIAL/'result.json').read_text(encoding='utf-8'));frozen=json.loads((TRIAL/'frozen.json').read_text(encoding='utf-8'))
expected={(x['store'],x['sku']):x for x in approved['changes']}
allowed={'BP','BQ','BR','BT','BU','AS','BV','BW','BX','BM','资金释放比例','实际库存金额上限'}
out=QA/'real';out.mkdir(exist_ok=False);started=time.perf_counter();stores=[];differences=[];risks=[];rows=0;replayed=0;hits=0;zero=0
for item in frozen['orders']:
    file=TRIAL/'frozen'/item['file'];assert hashlib.sha256(file.read_bytes()).hexdigest()==item['sha256']
    order=pickle.loads(zlib.decompress(file.read_bytes()));r=order['payload']['result'];params=r['config']['parameters'];new_rows=[];seen=set()
    for d in r['rows']:
        rows+=1;key=(r['store'],d['E']);assert key not in seen;seen.add(key);new=d
        if d['_source']=='资料匹配' and not d['_meta']['blocking']:
            before=old.calculate(dict(d),old.Rules(**params));after=model.calculate(dict(d),model.Rules(**params));replayed+=1
            raw={k:[before.get(k),after.get(k)] for k in set(before)|set(after) if wr.dumps(before.get(k))!=wr.dumps(after.get(k))}
            assert set(raw)<=allowed|{'_inbound_cap'},(key,set(raw)-allowed)
            if not raw:
                a0,audit0=oldbox.apply_one_box_exception(before,d['_meta'],enabled=r['enabled'])
                a1,audit1=box.apply_one_box_exception(after,d['_meta'],enabled=r['enabled'])
                assert not diff(a0,a1) and not diff(audit0,audit1),('non-target wrapper',key)
            if raw:
                assert d['L']>0 and after.get('_inbound_cap'),(key,raw)
                hits+=1
                assert before['AS']==d['_base_qty'] and before['BV']==d['_base_amount'],key
                assert after['_inbound_cap']['previous_suggestion']==before['AS'],key
                final,audit=box.apply_one_box_exception(after,d['_meta'],enabled=r['enabled'])
                new=copy.deepcopy(d)
                for k in model.CALCULATED+['资金释放比例','实际库存金额上限']:
                    if k in final:new[k]=final[k]
                    elif k in new and k in allowed:new.pop(k)
                new['_inbound_cap']=after['_inbound_cap'];new.update(_audit=audit,_base_qty=after['AS'],_base_amount=after['BV'])
                new['_candidate_qty']=0 if sem.exclusion_reason(new) else sem.quantity(new);new['_candidate_amount']=0 if sem.exclusion_reason(new) else sem.amount_value(new)
                differences.append({'store':key[0],'sku':key[1],'fields':{k:[d.get(k),new.get(k)] for k in set(d)|set(new) if wr.dumps(d.get(k))!=wr.dumps(new.get(k))}})
            else:assert not diff(before,after),(key,diff(before,after))
        q0,q1=sem.order_quantity(d),sem.order_quantity(new)
        if key in expected:
            target=expected[key];assert q0==target['old_qty'] and q1==target['new_qty'];assert abs(sem.order_amount(d)-sem.order_amount(new)-target['reduced_amount'])<1e-9
        else:assert wr.dumps([q0,sem.order_amount(d)])==wr.dumps([q1,sem.order_amount(new)]),key
        if model.number(q1) and q1>0:assert abs(q1/new['BD']-round(q1/new['BD']))<1e-8,key
        if all(model.number(d.get(k)) for k in ['H','I','J','K','L']) and max(d['H'],d['I'],d['J'])>0 and d['K']==d['L']==0:
            assert wr.dumps(d)==wr.dumps(new);zero+=1
        risk=sem.inbound_risk(new)
        if risk:risks.append({'store':key[0],'sku':key[1],**risk})
        new_rows.append(new)
    assert not r['budget']['enabled']
    assert not input_adapter.business_checks(new_rows,model.Rules(**params))['failures']
    nr={**r,'rows':new_rows,'summary':sem.summarize(new_rows)}
    stores.append({'store':r['store'],'before':r['summary']['amount'],'after':nr['summary']['amount']})
    order['payload']['result']=nr;(out/item['file']).write_bytes(zlib.compress(pickle.dumps(order,protocol=5)))
    print(r['store'],stores[-1],flush=True)
assert rows==164722 and hits==306 and zero==5567
assert {(x['store'],x['sku']) for x in risks}=={(x['store'],x['sku']) for x in approved['changes'] if x['next_arrival_margin']<0}
assert round(sum(x['after'] for x in stores),2)==237962.58
report={'passed':True,'baseline_commit':frozen['source_commit'],'rows':rows,'full_model_replays':replayed,'cap_hits':hits,'quantity_changes':len(expected),'non_target_quantity_amount_differences':0,'both_zero_unchanged':zero,'risks':risks,'stores':stores,'before':240467.53,'after':237962.58,'reduction':2504.95,'duration_seconds':time.perf_counter()-started,'allowed_calculation_fields':sorted(allowed),'new_audit_field':'_inbound_cap','differences':differences}
report['source_hashes']={name:hashlib.sha256((ROOT/'src'/name).read_bytes()).hexdigest() for name in ['model.py','one_box_exception.py','result_semantics.py','input_adapter.py','allocation.py','default_config.json','default_must_sell.json']}
(out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps({k:v for k,v in report.items() if k not in ['differences','stores','risks']},ensure_ascii=False))
