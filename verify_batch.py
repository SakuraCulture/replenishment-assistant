"""Real-data equivalence plus clearly labelled synthetic multi-store fixtures."""
import argparse,copy,hashlib,json,multiprocessing,subprocess,sys,threading
from pathlib import Path
root=Path(__file__).resolve().parent;sys.path.insert(0,str(root/'src'))
import engine,batch_service,priority_list,openpyxl
from version_info import VERSION

def write_fixture(path,headers,rows):
    book=openpyxl.Workbook(write_only=True);ws=book.create_sheet('补货建议');ws.append(headers)
    for row in rows:
        cells=[]
        for h in headers:
            cell=openpyxl.cell.WriteOnlyCell(ws,row.get(h))
            if isinstance(cell.value,str):cell.data_type='s'
            cells.append(cell)
        ws.append(cells)
    book.save(path);book.close()

def signature(result):
    return [(d['E'],d['AS'],d['BV'],engine.order_quantity(d),engine.order_amount(d)) for d in result['rows']]

def main():
    p=argparse.ArgumentParser();p.add_argument('--sales',type=Path,required=True);p.add_argument('--products',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    for path in [a.sales,a.products]:assert path.is_file()
    commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip()
    assert not subprocess.check_output(['git','status','--porcelain'],cwd=root,text=True).strip(),'Commit source before release verification'
    out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
    digest=lambda path:hashlib.sha256(path.read_bytes()).hexdigest()
    hashes={str(x):digest(x) for x in [a.sales,a.products]};checks=[]
    profile=json.loads((root/'src/default_must_sell.json').read_text(encoding='utf-8'))
    single=engine.load_inputs(a.sales,a.products);code=next(iter(single['stores']))
    ordinary=engine.run_calculation(single,code,engine.DEFAULT_WAREHOUSES,True,None,profile)
    imported=batch_service.load([a.sales],a.products)
    batchsingle=engine.run_calculation(imported,code,engine.DEFAULT_WAREHOUSES,True,None,profile)
    assert signature(ordinary)==signature(batchsingle);assert ordinary['summary']==batchsingle['summary']
    checks.append('real single-file import and 14244-row calculation equals original engine')
    sheet,heads=engine.sheet_name(a.sales,'sales');raw,_=engine.adapter.read_table(a.sales,sheet,['商品编码','仓库/门店编码']);headers=[h for h in raw[0] if h!='_row']
    other=copy.deepcopy(raw)
    for r in other:r['仓库/门店编码']='QA_STORE_B';r['仓库/门店名称']='测试门店B（合成）'
    write_fixture(out/'multi.xlsx',headers,raw+other)
    write_fixture(out/'part1.xlsx',headers,raw[:len(raw)//2]+other[:len(other)//2])
    write_fixture(out/'part2.xlsx',headers,raw[len(raw)//2:]+other[len(other)//2:]+[raw[0]])
    multi=batch_service.load([out/'multi.xlsx'],a.products)
    split=batch_service.load([out/'part1.xlsx',out/'part2.xlsx'],a.products)
    assert split['intake']['duplicates']==1 and len(split['normalized'])==len(multi['normalized'])==2*len(raw)
    normalized=lambda data:sorted((d['B'],d['E'],json.dumps(d,sort_keys=True,default=str)) for d,m in data['normalized'])
    assert normalized(multi)==normalized(split)
    checks.append('one multi-store workbook equals multiple mixed-store workbooks; identical rows deduplicated')
    conflicting=copy.deepcopy(raw[0]);conflicting['可用库存']=9999
    write_fixture(out/'conflict.xlsx',headers,[conflicting])
    data=batch_service.intake([a.sales,out/'conflict.xlsx'],a.products);assert len(data['conflicts'])==1
    blocked=batch_service.prepare(data);assert blocked['blocked'][code]==1
    data['conflicts'][0]['choice']=0;resolved=batch_service.prepare(data);assert not resolved['blocked'];assert normalized(resolved)==normalized(imported)
    # Duplicate within one file must be resolved before the adapter (it otherwise refuses matching).
    write_fixture(out/'duplicates.xlsx',headers,[raw[0],raw[0],conflicting])
    data=batch_service.intake([out/'duplicates.xlsx'],a.products);assert data['duplicates']==1 and len(data['conflicts'])==1
    data['conflicts'][0]['choice']=0;resolved=batch_service.prepare(data);assert len(resolved['normalized'])==1;assert resolved['normalized'][0][1]['match']==single['normalized'][0][1]['match']
    checks.append('cross-file and intra-file conflicts block store; explicit choice restores exact master matching')
    settings=lambda b:{'warehouses':engine.DEFAULT_WAREHOUSES,'exception':True,'budget':b}
    jobs={code:settings(3000),'QA_STORE_B':settings(5000)};events=[]
    parallel=batch_service.run(multi,jobs,profile,2,lambda *event:events.append(event[:2]))
    for s,config in jobs.items():
        serial=batch_service.calculate_store(multi,s,config,profile);assert signature(serial)==signature(parallel[s]);assert serial['summary']==parallel[s]['summary']
    times=[r['task'] for r in parallel.values()];assert len({x['pid'] for x in times})==2
    assert max(x['started'] for x in times)<min(x['finished'] for x in times),'tasks did not overlap'
    checks.append('two distinct processes overlap; every SKU quantity/amount equals serial computation')
    isolated=batch_service.run(multi,{code:settings(0),'QA_STORE_B':settings(5000)},profile,2)
    assert isolated[code]['summary']['amount']==0;assert signature(isolated['QA_STORE_B'])==signature(parallel['QA_STORE_B'])
    checks.append('changing one store budget to zero leaves every other store quantity and amount unchanged')
    failed_events=[];failure=batch_service.run(multi,{'QA_MISSING':settings(None),code:settings(0)},profile,2,lambda *e:failed_events.append(e[:2]))
    assert failure['QA_MISSING'] is None and failure[code]['summary']['amount']==0
    stop=threading.Event();stop_events=[]
    def notify(s,state,result,error):
        stop_events.append((s,state))
        if state=='已完成':stop.set()
    stopped=batch_service.run(multi,jobs,profile,1,notify,stop)
    assert list(stopped)==[code] and ('QA_STORE_B','已停止') in stop_events
    checks.append('one store failure does not stop another; stop preserves running result and never starts queued store')
    statuses={s:{'name':r['store_name'],'state':'已完成','error':''} for s,r in parallel.items()};statuses['FAILED']={'name':'失败测试','state':'计算失败','error':'测试原因'}
    exported=out/'batch_export.xlsx';batch_service.export_batch(parallel,statuses,exported)
    book=openpyxl.load_workbook(exported,read_only=True,data_only=False)
    rows=list(book['门店汇总'].iter_rows(values_only=True,max_col=15));assert rows[-1][6] is None and rows[-1][2]=='计算失败'
    amounts={};it=book['补货清单'].values;columns=next(it)
    for values in it:
        row=dict(zip(columns,values));assert row['商品资料状态']!='禁用' and row['补货建议状态']!='停采';amounts[row['门店编码']]=amounts.get(row['门店编码'],0)+row['执行金额']
    assert all(round(amounts[s],2)==r['summary']['amount'] for s,r in parallel.items());book.close()
    try:batch_service.export_batch(parallel,statuses,exported)
    except ValueError:pass
    else:raise AssertionError('existing export overwritten')
    stale={s:{**state,'state':'待重算'} for s,state in statuses.items()}
    try:batch_service.export_batch(parallel,stale,out/'stale.xlsx')
    except ValueError:pass
    else:raise AssertionError('stale export accepted')
    batch_service.export_batch(parallel,statuses,out/'per_store',True)
    assert len(list((out/'per_store').glob('*.xlsx')))==3
    checks.append('combined/per-store exports reconcile; failed amounts blank; exclusions retained; stale and overwrite exports refused')
    assert hashes=={str(x):digest(x) for x in [a.sales,a.products]}
    assert commit==subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip()
    assert not subprocess.check_output(['git','status','--porcelain'],cwd=root,text=True).strip()
    report={'passed':True,'version':VERSION,'source_commit':commit,'checks':checks,'real_data_summary':ordinary['summary'],'concurrency':times,'budget_results':{s:r['summary'] for s,r in parallel.items()},'input_hashes_unchanged':hashes,'fixture_note':'QA_STORE_B is a synthetic duplicate of actual store rows, not a second real store observation'}
    (out/'batch_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(report,ensure_ascii=False,indent=2))

if __name__=='__main__':multiprocessing.freeze_support();main()
