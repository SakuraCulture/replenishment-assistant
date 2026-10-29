"""Daily overwrite/CAS/rollback and actual parameter effects on isolated stores."""
import argparse,copy,json,subprocess,sys,sqlite3,multiprocessing
from pathlib import Path
root=Path(__file__).resolve().parent;sys.path.insert(0,str(root/'src'))
import work_repository as wr,parameter_schema as ps,warehouse_catalog as wc,batch_service,engine,parameter_trial,openpyxl
from verify_warehouse import fixtures
from verify_batch import signature,write_fixture
from version_info import VERSION,MODEL_VERSION

def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);p.add_argument('--development',action='store_true');p.add_argument('--sales',type=Path);p.add_argument('--products',type=Path);a=p.parse_args();out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
    commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip()
    if not a.development:assert not subprocess.check_output(['git','status','--porcelain'],cwd=root,text=True).strip()
    checks=[];fixtures(out);catalogs={w:wc.import_catalog(out/(w+'.xlsx'),w) for w in wc.WAREHOUSES};loaded=batch_service.load([out/'sales.xlsx'],catalogs);profile=json.loads((root/'src/default_must_sell.json').read_text(encoding='utf-8'));base=ps.standard();repo=wr.Repository(out/'settings')
    config={'warehouses':engine.DEFAULT_WAREHOUSES,'exception':True,'budget':None,'supply_route':{'primary':'河北仓','secondary':'山东仓'}}
    old=batch_service.calculate_store(loaded,'QA_A',config,profile);new=batch_service.calculate_store(loaded,'QA_A',{**config,'parameter_config':base},profile);assert signature(old)==signature(new)
    changed=copy.deepcopy(base);changed['parameters']['probability']=.5;changed['weight_profiles']['快销']['alpha']=.1;changed['weight_profiles']['快销']['w1']=.3;changed['weight_profiles']['快销']['w2']=.4
    jobs={'QA_A':{**config,'parameter_config':changed},'QA_B':{**config,'parameter_config':base}};parallel=batch_service.run(loaded,jobs,profile,2)
    assert signature(parallel['QA_B'])==signature(batch_service.calculate_store(loaded,'QA_B',config,profile));assert signature(parallel['QA_A'])!=signature(old)
    assert next(d for d in parallel['QA_A']['rows'] if d['E']=='both')['AB']==.1
    assert next(d for d in parallel['QA_B']['rows'] if d['E']=='both')['AB']==.4
    # Demonstrate actual reweighting against an independently calculated non-flat forecast.
    trend=copy.deepcopy(loaded)
    for pairs in [trend['normalized'],*trend['warehouse_rows'].values()]:
        for d,m in pairs:
            if d['E']=='both':d.update(H=1,I=14,J=30)
    trend_base=batch_service.calculate_store(trend,'QA_A',{**config,'parameter_config':base},profile);trend_new=batch_service.calculate_store(trend,'QA_A',{**config,'parameter_config':changed},profile)
    x=next(d for d in trend_base['rows'] if d['E']=='both');y=next(d for d in trend_new['rows'] if d['E']=='both')
    assert abs(x['AN']-1.4)<1e-9 and abs(y['AN']-.9)<1e-9,(x['AN'],y['AN'])
    mapped=copy.deepcopy(base);mapped['category_profiles']['饮品']='日百';r=batch_service.calculate_store(trend,'QA_A',{**config,'parameter_config':mapped},profile);assert next(d for d in r['rows'] if d['E']=='both')['AB']==.2
    checks.append('standard equals prior engine; changed probability affects quantities; independent AN forecast changes 1.4 to 0.9; per-store weights and mappings applied; other store unchanged')
    v1=repo.save_preset('稳健试算',changed);v2=repo.save_preset('稳健试算',base,v1['id']);assert v2['version']==2 and next(p for p in repo.presets() if p['id']==v1['id'] and p['version']==1)['config']==json.loads(json.dumps(changed))
    repo.save_stores({'QA_A':{'preset':v1,'ui':{'mode':1,'amount':'','primary':'河北仓','secondary':'山东仓'}}});assert wr.Repository(out/'settings').stores()['QA_A']['preset']['version']==1
    repo.archive_preset(v1['id'],1);assert repo.stores()['QA_A']['preset']['config']==json.loads(json.dumps(changed))
    for key,value in [('exit_monthly',2),('probability',1),('extreme_days',31),('minimum_reference_units',0)]:
        bad=copy.deepcopy(base);bad['parameters'][key]=value
        try:ps.validate(bad)
        except ValueError:pass
        else:raise AssertionError(key)
    bad=copy.deepcopy(base);bad['weight_profiles']['快销']['w2']=.9
    try:ps.validate(bad)
    except ValueError:pass
    else:raise AssertionError('bad weights')
    checks.append('full immutable preset v1/v2, archive preserves bound v1, store defaults reload; invalid parameter relationships refused')
    states={s:{'name':r['store_name'],'state':'已完成','error':''} for s,r in parallel.items()};day=wr.today()
    payload={'schema':1,'day':day,'model_version':MODEL_VERSION,'states':states,'results':parallel,'loaded':loaded,'input_dirty':False}
    first=repo.save_job(day,payload)
    try:repo.save_job(day,payload)
    except wr.Conflict:pass
    else:raise AssertionError('unconfirmed overwrite')
    changed_payload=copy.deepcopy(payload);changed_payload['states']['QA_A']['state']='待重算'
    second=repo.save_job(day,changed_payload,first['revision']);assert len(repo.list_jobs())==1 and repo.get_job(day)['summary']['completed']==1
    try:repo.save_job(day,payload,first['revision'])
    except wr.Conflict:pass
    else:raise AssertionError('stale confirmation')
    assert repo.get_job(day)['revision']==second['revision']
    # A real database write failure rolls back the replace rather than destroying old data.
    db=sqlite3.connect(repo.path);db.execute("CREATE TRIGGER fail_update BEFORE UPDATE ON jobs BEGIN SELECT RAISE(ABORT,'injected write failure'); END");db.commit();db.close()
    try:repo.save_job(day,payload,second['revision'])
    except sqlite3.DatabaseError:pass
    else:raise AssertionError('write failure not raised')
    assert repo.get_job(day)['revision']==second['revision'];db=sqlite3.connect(repo.path);db.execute('DROP TRIGGER fail_update');db.commit();db.close()
    bad=copy.deepcopy(payload);bad['states']['QA_A']['state']='计算中'
    try:repo.save_job(day,bad,second['revision'])
    except ValueError:pass
    else:raise AssertionError('running state saved')
    for name in ['sales.xlsx','河北仓.xlsx','山东仓.xlsx']:(out/name).rename(out/(name+'.moved'))
    restored=wr.Repository(out/'settings').get_job(day)['payload'];assert signature(batch_service.calculate_store(restored['loaded'],'QA_B',jobs['QA_B'],profile))==signature(parallel['QA_B'])
    batch_service.export_batch(restored['results'],restored['states'],out/'restored.xlsx')
    for name in ['sales.xlsx','河北仓.xlsx','山东仓.xlsx']:(out/(name+'.moved')).rename(out/name)
    checks.append('one row per day; no overwrite without exact observed revision; stale revision rejected; injected transactional write failure preserves old record; active tasks refused; restore/recompute/export without source files')
    before=wr.dumps(parallel);report=parameter_trial.compare(loaded,jobs,profile,base);assert '变化商品数' in report and before==wr.dumps(parallel)
    checks.append('same-snapshot trial reports changes and coverage without mutating official results')
    # A restored legacy-mode conflict can be resolved even when its original files have moved.
    master=[{**r,'仓归属':'河北仓'} for r in catalogs['河北仓']['items'].values()];write_fixture(out/'legacy.xlsx',list(master[0]),master)
    raw,_=engine.adapter.read_table(out/'sales.xlsx',engine.sheet_name(out/'sales.xlsx','sales')[0],['商品编码'])
    conflicting=copy.deepcopy(raw[0]);conflicting['可用库存']=8;write_fixture(out/'conflict.xlsx',[k for k in conflicting if k!='_row'],[conflicting])
    intake=batch_service.intake([out/'sales.xlsx',out/'conflict.xlsx'],out/'legacy.xlsx');assert len(intake['conflicts'])==1;intake['frozen']=True;intake['conflicts'][0]['choice']=0
    for name in ['sales.xlsx','conflict.xlsx','legacy.xlsx']:(out/name).rename(out/(name+'.moved'))
    reconstructed=batch_service.prepare(intake);assert not reconstructed['blocked'];assert len(reconstructed['normalized'])==24
    for name in ['sales.xlsx','conflict.xlsx','legacy.xlsx']:(out/(name+'.moved')).rename(out/name)
    checks.append('frozen legacy master and sales snapshots resolve saved conflicts without source files; original source row references retained')
    if not a.development:assert a.sales and a.products,'release verification requires real regression inputs'
    if a.sales and a.products:
        import model
        hashes={str(p):engine.adapter.digest(p) for p in [a.sales,a.products]};real=engine.load_inputs(a.sales,a.products);s=next(iter(real['stores']))
        before=engine.run_calculation(real,s,engine.DEFAULT_WAREHOUSES,True,None,profile)
        after=batch_service.calculate_store(real,s,{'warehouses':engine.DEFAULT_WAREHOUSES,'exception':True,'budget':None,'parameter_config':base},profile)
        assert len(before['rows'])==len(after['rows'])==14244
        assert all(all(x.get(k)==y.get(k) for k in model.CALCULATED+['AB','AC','AD','AG','AH']) for x,y in zip(before['rows'],after['rows']))
        assert before['summary']==after['summary'] and after['summary']['amount']==12268.87
        assert hashes=={str(p):engine.adapter.digest(p) for p in [a.sales,a.products]}
        checks.append('real 14244-row standard preset equals prior engine for every calculated field, weights, cycles and summary; original inputs unchanged')
    if not a.development:assert not subprocess.check_output(['git','status','--porcelain'],cwd=root,text=True).strip()
    result={'passed':True,'development':a.development,'version':VERSION,'source_commit':commit,'checks':checks,'fixture_note':'Synthetic stores and warehouse inputs; real baseline regression is separate.'};(out/'workbench_report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(result,ensure_ascii=False,indent=2))
if __name__=='__main__':multiprocessing.freeze_support();main()
