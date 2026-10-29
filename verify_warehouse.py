"""Warehouse demand ownership acceptance; all dual-warehouse cases are synthetic."""
import argparse,copy,hashlib,json,multiprocessing,subprocess,sys
from pathlib import Path
root=Path(__file__).resolve().parent;sys.path.insert(0,str(root/'src'))
import engine,warehouse_catalog as wc,batch_service,openpyxl
from version_info import VERSION
from verify_batch import write_fixture,signature

def fixtures(out):
    # No internal warehouse column: the explicit import destination is authoritative.
    cases=['both','disabled','empty','missing','both_empty','both_disabled','child_missing','unknown','small','stopped','standby','local']
    for wh,price,box in [('河北仓',2,1),('山东仓',3,3)]:
        rows=[]
        for sku in cases:
            if sku=='local' or (wh=='河北仓' and sku=='missing') or (wh=='山东仓' and sku=='child_missing'):continue
            stock=100;status='启用'
            if sku in ['both_empty','child_missing'] or (sku=='empty' and wh=='河北仓'):stock=0
            if sku=='both_disabled' or (sku=='disabled' and wh=='河北仓'):status='禁用'
            if sku=='unknown' and wh=='河北仓':stock=None
            if sku=='small':stock=1
            if sku=='standby':status='备用'
            rows.append({'商品编码':sku,'商品名称':'合成测试-'+sku,'分类':'饮品' if wh=='河北仓' else '家居日用','基本售价':price,'采购价':999,'出库箱规':box,'商品状态':status,'可用数':stock})
        write_fixture(out/(wh+'.xlsx'),list(rows[0]),rows)
    rows=[]
    for store in ['QA_A','QA_B']:
        for sku in cases:
            rows.append({'仓库/门店编码':store,'仓库/门店名称':'合成门店'+store,'商品编码':sku,'商品名称':'合成测试-'+sku,'日销':2,'周销':14,'月销':60,'可用库存':0,'在途库存':0,'缺货天数（天）':0,'补货状态':'停采' if sku=='stopped' else '正常','基础单位':'个'})
    write_fixture(out/'sales.xlsx',list(rows[0]),rows)

def main():
    p=argparse.ArgumentParser();p.add_argument('--sample',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--development',action='store_true');a=p.parse_args()
    commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip()
    if not a.development:assert not subprocess.check_output(['git','status','--porcelain'],cwd=root,text=True).strip()
    out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);checks=[];digest=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
    sha=digest(a.sample);sample=wc.import_catalog(a.sample,'河北仓')
    assert len(sample['items'])==9142;assert sha==digest(a.sample)
    checks.append('provided 9142-row template imports read-only; warehouse label here is only a test designation')
    fixtures(out);catalogs={w:wc.import_catalog(out/(w+'.xlsx'),w) for w in wc.WAREHOUSES};before=copy.deepcopy(catalogs)
    repository=wc.Repository(out/'settings')
    for c in catalogs.values():repository.save(c)
    assert repository.catalogs()==catalogs
    bad=copy.deepcopy(catalogs['河北仓']);bad['items']['both']['基本售价']=999
    try:repository.save(bad)
    except ValueError:pass
    else:raise AssertionError('invalid revision saved')
    row=catalogs['河北仓']['items']['both'];write_fixture(out/'duplicate.xlsx',list(row),[row,row])
    try:wc.import_catalog(out/'duplicate.xlsx','河北仓')
    except ValueError:pass
    else:raise AssertionError('duplicate master accepted')
    assert repository.catalogs()==catalogs
    checks.append('cross-warehouse overlap allowed; within-warehouse duplicates and corrupt snapshots rejected, old catalog preserved')
    loaded=batch_service.load([out/'sales.xlsx'],catalogs)
    profile=json.loads((root/'src/default_must_sell.json').read_text(encoding='utf-8'))
    def settings(primary='河北仓',secondary='山东仓',budget=None):return {'warehouses':engine.DEFAULT_WAREHOUSES,'exception':True,'budget':budget,'supply_route':{'primary':primary,'secondary':secondary}}
    aresult=batch_service.calculate_store(loaded,'QA_A',settings(),profile);rows={d['E']:d for d in aresult['rows']}
    for sku in ['both','unknown','small','standby']:assert rows[sku]['AU']=='河北仓',(sku,rows[sku])
    for sku in ['disabled','empty','missing','both_empty','both_disabled','child_missing']:assert rows[sku]['AU']=='山东仓',(sku,rows[sku])
    for sku in ['disabled','empty','missing','both_empty']:
        d=rows[sku];assert d['V']==3 and d['S']=='启用' and engine.order_quantity(d)>0 and engine.order_quantity(d)%3==0,d
    assert rows['both']['V']==2 and rows['small']['AS']>1 and engine.order_quantity(rows['small'])>1
    assert rows['both_disabled']['AS']>0 and engine.order_quantity(rows['both_disabled'])==0
    assert engine.order_quantity(rows['stopped'])==0
    assert rows['child_missing']['_source']=='匹配待核' and not engine.numeric(rows['child_missing'].get('V'))
    assert rows['local']['_source']=='地采商品' and rows['local']['AU']=='地采'
    assert '待核' in rows['unknown']['_meta']['routing']['reason']
    assert rows['both']['AB']==0.4 and rows['empty']['AB']==0.2 and rows['both']['AH']==4 and rows['empty']['AH']==5
    checks.append('primary priority, disabled/empty/missing fallback, secondary zero-stock demand retained, secondary disabled and stopped excluded, unknown stock retained, no split/cap')
    single=batch_service.calculate_store(loaded,'QA_A',settings(secondary=None),profile);sr={d['E']:d for d in single['rows']}
    assert sr['both_empty']['AU']=='河北仓' and engine.order_quantity(sr['both_empty'])>0
    assert sr['missing']['_source']=='匹配待核' and sr['missing']['AU']=='河北仓'
    checks.append('single-warehouse zero stock does not suppress demand; SKU in unconfigured warehouse is pending, never silently local')
    for badroute in [{'primary':'河北仓','secondary':'河北仓'},{'primary':None,'secondary':None}]:
        try:wc.validate_route(badroute,catalogs)
        except ValueError:pass
        else:raise AssertionError('invalid supply route accepted')
    pending=copy.deepcopy(loaded)
    pending['normalized']=[p for p in pending['normalized'] if p[0]['E']=='local']
    pending['warehouse_rows']={w:[p for p in pairs if p[0]['E']=='local'] for w,pairs in pending['warehouse_rows'].items()}
    local_only=batch_service.calculate_store(pending,'QA_A',settings(),profile)
    assert local_only['summary']['local']==1 and local_only['summary']['calculable']==0 and not engine.numeric(engine.order_quantity(local_only['rows'][0]))
    engine.export_xlsx(local_only,out/'all_local_pending.xlsx')
    checks.append('invalid supply routes rejected; all-local store stays explicitly uncalculated and exportable')
    jobs={'QA_A':settings(budget=60),'QA_B':settings('山东仓','河北仓',80)}
    parallel=batch_service.run(loaded,jobs,profile,2)
    for s,c in jobs.items():
        serial=batch_service.calculate_store(loaded,s,c,profile);assert signature(serial)==signature(parallel[s]);assert serial['summary']['amount']<=c['budget']
        assert all(engine.order_quantity(d)%d['BD']==0 for d in serial['rows'] if engine.orderable(d))
    assert {d['E']:d for d in parallel['QA_B']['rows']}['both']['V']==3
    assert catalogs==before
    checks.append('opposite store priorities price independently; serial/process quantities equal; one total store budget across warehouses; cartons preserved; stock never reserved')
    statuses={s:{'name':r['store_name'],'state':'已完成','error':''} for s,r in parallel.items()}
    batch_service.export_batch(parallel,statuses,out/'combined.xlsx');batch_service.export_batch(parallel,statuses,out/'per_store',True)
    book=openpyxl.load_workbook(out/'combined.xlsx',read_only=True);it=book['全部明细'].values;headers=next(it);detail=[dict(zip(headers,r)) for r in it]
    assert {'主仓','从仓','需求归属说明','商品资料来源','仓资料版本'}<=set(headers)
    assert next(d for d in detail if d['门店编码']=='QA_A' and d['商品编码']=='both_empty')['所属仓']=='山东仓'
    sums={s:0 for s in parallel}
    for values in list(book['分仓需求汇总'].values)[1:]:sums[values[0]]+=values[6]
    assert all(round(sums[s],2)==r['summary']['amount'] for s,r in parallel.items());book.close()
    checks.append('combined/per-store workbook provenance and warehouse subtotals reconcile to total budget spend')
    for s,c in jobs.items():repository.save_route(s,c['supply_route'])
    assert repository.routes()=={s:c['supply_route'] for s,c in jobs.items()}
    for w in wc.WAREHOUSES:
        path=out/(w+'.xlsx');path.rename(out/(w+'_source_moved.xlsx'))
    restored=repository.catalogs();again=batch_service.load([out/'sales.xlsx'],restored)
    assert signature(batch_service.calculate_store(again,'QA_A',settings(),profile))==signature(aresult)
    for w in wc.WAREHOUSES:(out/(w+'_source_moved.xlsx')).rename(out/(w+'.xlsx'))
    checks.append('saved catalogs and per-store priorities survive reload without original master files')
    assert digest(a.sample)==sha
    if not a.development:assert commit==subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip() and not subprocess.check_output(['git','status','--porcelain'],cwd=root,text=True).strip()
    report={'passed':True,'development':a.development,'version':VERSION,'source_commit':commit,'checks':checks,'sample_sha256':sha,'fixture_note':'Only one real master template provided; all warehouse labels, two-warehouse stock and store cases are synthetic, not observed supply availability.','unlimited_summary':aresult['summary'],'budget_summary':{s:r['summary'] for s,r in parallel.items()}}
    (out/'warehouse_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(report,ensure_ascii=False,indent=2))

if __name__=='__main__':multiprocessing.freeze_support();main()
