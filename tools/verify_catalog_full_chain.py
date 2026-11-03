"""Real downloaded catalogs through ordinary calculation/sign/lifecycle APIs.

Only --output is mutated. Sales are the frozen 14244-row regression input;
lifecycle documents are explicitly synthetic quantities derived from its result.
No browser or user settings are used by this replay.
"""
import argparse,copy,hashlib,json,shutil,sys,time,sqlite3
from contextlib import closing
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tools')]
import warehouse_catalog as wc,work_repository as wr,normal_orders as no
import batch_service as bs,parameter_schema as ps,new_product_focus as focus
import normal_exports as ne,archive_batch as ab,purchases,order_lifecycle as lc,lifecycle_io as io
import catalog_sync as sync,source_archive,jst_orders as jst,openpyxl
from version_info import MODEL_VERSION

def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def require(ok,message):
    if not ok:raise AssertionError(message)
def main():
    p=argparse.ArgumentParser();p.add_argument('--live',type=Path,required=True);p.add_argument('--inputs',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    out=a.output.resolve();require(out.is_relative_to(ROOT/'qa'),'QA only');out.mkdir(parents=True,exist_ok=False)
    shutil.copytree(a.live/'settings',out/'settings')
    settings=out/'settings';work=wr.Repository(settings);repo=no.Repository(work);catalogs=wc.Repository(settings).catalogs()
    require(set(catalogs)=={'山东仓','河北仓'},'Both adopted catalogs required')
    checks=[];times={};sources={}
    for wh,c in catalogs.items():
        with closing(work.connect()) as db:stored=source_archive.read(db,c['source_sha256'])
        require(stored is not None and hashlib.sha256(stored[1]).hexdigest()==c['source_sha256'],'Raw snapshot')
        manual=wc.import_catalog(c['source_path'],wh)
        require(wr.dumps(manual['items'])==wr.dumps(c['items']),'Manual parser strict type/order equality')
        sources[wh]={'rows':len(c['items']),'sha256':c['source_sha256'],'revision':c['revision']}
        jst.commit_refresh(work.path,jst.prepare_refresh(work.path,wh,catalog=c))
    checks.append('Both real originals retained exactly; manual parser values/types/order equal')
    state_before={wh:sync.stock_observation(work.path,wh) for wh in catalogs}
    t=time.perf_counter();loaded=bs.load([a.inputs/'补货建议导出服务.xlsx'],catalogs)
    profile=json.loads((a.inputs/'must.json').read_text('utf-8'));config=ps.standard();results={}
    for code in loaded['stores']:
        results[code]=bs.calculate_store(loaded,code,{'warehouses':config['warehouses'],'exception':config['exception'],'budget':None,'parameter_config':config,'supply_route':{'primary':'河北仓','secondary':'山东仓'}},profile)
    times['prepare_and_compute']=time.perf_counter()-t
    payload={'schema':1,'day':wr.today(),'model_version':MODEL_VERSION,'input_dirty':False,'loaded':loaded,'catalogs':catalogs,'profile':profile,'results':results,'states':{s:{'state':'已完成','name':r['store_name']} for s,r in results.items()},'new_focus':focus.empty(wr.today()[:7])}
    t=time.perf_counter();saved=work.save_job(wr.today(),payload)
    require(wr.dumps(work.get_job(wr.today())['payload'])==wr.dumps(payload),'Daily job full round trip')
    orders=repo.confirm(payload,list(results),saved['revision'],{s:None for s in results});times['save_and_sign']=time.perf_counter()-t
    heads=purchases.targets(work.path);require({h['warehouse'] for h in heads}==set(catalogs),'Both warehouse orders')
    signed_before={s:no.fingerprint(repo.get(wr.today(),s)['payload']['result']) for s in results}
    for h in heads:
        order=repo.get(h['day'],h['store'],h['revision']);c,stored=ab.source(repo,order,h['warehouse'])
        require(c['source_sha256']==sources[h['warehouse']]['sha256'],'Signed catalog version')
        ne.export(order,out/(h['warehouse']+'-签发单.xlsx'),warehouse=h['warehouse'])
    checks.append('Real sales compute, daily save/restore, sign and both warehouse exports succeeded')
    # Explicitly synthetic lifecycle forms. Never treat these as actual orders.
    t=time.perf_counter();stages={}
    for stage in io.STAGES:
        path=out/('合成验收-'+stage+'.xlsx');book=openpyxl.Workbook();ws=book.active;ws.append(io.fields(stage))
        for h in heads:
            rows=repo.get(h['day'],h['store'])['payload']['result']['rows']
            from result_semantics import order_quantity,order_amount
            selected=[d for d in rows if d.get('AU')==h['warehouse'] and isinstance(order_quantity(d),(int,float)) and order_quantity(d)>0]
            require(bool(selected),'Positive issued goods in each warehouse')
            for d in selected[:3]:
                row={'补货单号':h['number'],'门店编码':h['store'],'供货仓':h['warehouse'],'商品编码':d['E'],'商品名称':d['D'],'数量单位':'库存单位',io.QUANTITIES[stage]:order_quantity(d),io.DOCNUM[stage]:'QA-'+stage,io.DOCDATE[stage]:wr.today(),'实际金额':order_amount(d),'币种':'CNY'}
                ws.append([row.get(f) for f in io.fields(stage)])
        book.save(path);book.close();file=io.inspect([path])[0];sheet=file['sheets'][0]
        scan=io.scan(stage,[{**file,**sheet,'mapping':{h:i for i,h in enumerate(sheet['headers'])}}])
        lc.commit(work.path,lc.prepare_numbered(work.path,scan))
        stages[stage]=[lc.snapshot(work.path,h)['status'] for h in heads]
    times['synthetic_lifecycle_imports']=time.perf_counter()-t
    checks.append('Synthetic return/payment/receipt import by warehouse order number; issued facts unchanged')
    # Defaults may advance without changing stock already allocated or signed facts.
    for wh,c in catalogs.items():
        later=copy.deepcopy(c);later['imported_at']='QA later default metadata';wc.Repository(settings).save(later)
    for wh in catalogs:
        current=sync.stock_observation(work.path,wh)
        require(all(current[k]==state_before[wh][k] for k in ('state','included')),'Default update must not reset allocation baseline')
        require(jst.catalogs(work.path,settings)[wh]['catalog']==catalogs[wh],'Existing allocation catalog stays fixed')
    for s in results:require(no.fingerprint(repo.get(wr.today(),s)['payload']['result'])==signed_before[s],'Issued result immutable')
    for h in heads:
        order=repo.get(h['day'],h['store'],h['revision']);historical,stored=ab.source(repo,order,h['warehouse'])
        require(historical==catalogs[h['warehouse']],'History uses original catalog, not latest default')
    original=ab.execute(work.path,ab.prepare(work.path,heads,'original','combined'),out)
    require(not original.get('failed') and len(original['files'])==2,'Two original source exports')
    for f in original['files']:require(sha(f['path'])==sources[f['heads'][0]['warehouse']]['sha256'],'Exported raw bytes exact')
    checks.append('Default replacement leaves inventory baseline and signed snapshots fixed; original exports byte-identical')
    # Reuse exact validated real files to exercise the other adoption purpose.
    # This is local replay, not another native platform export.
    state=json.loads((a.live/'service/jobs.json').read_text('utf-8'));batch=copy.deepcopy(next(iter(state['batches'].values())))
    batch['purpose']='allocation_baseline';ad=sync.Adoptions(settings,work.path,a.live/'service')
    defaults={wh:wc.Repository(settings).current_digest(wh) for wh in catalogs}
    for item in batch['items']:
        wh=item['account']['warehouse'];item['observation']=sync.stock_observation(work.path,wh)
        ack=ad.apply(batch,item);require(ack['purpose']=='allocation_baseline','Allocation adoption purpose')
        require(wc.Repository(settings).current_digest(wh)==defaults[wh],'Allocation refresh must not replace default')
        with closing(work.connect()) as db:
            raw=db.execute('SELECT content FROM jst_files WHERE sha=?',(sources[wh]['sha256'],)).fetchone()
        require(raw is not None and hashlib.sha256(raw[0]).hexdigest()==sources[wh]['sha256'],'Allocation raw source retained')
    checks.append('Real-file replay of allocation-specific adoption retains its raw originals and does not replace defaults')
    report={'passed':True,'scope':'Real fresh dual-warehouse catalog + frozen real sales; synthetic lifecycle documents in private QA DB','checks':checks,'sources':sources,'stores':{s:{'rows':len(r['rows']),'summary':r['summary']} for s,r in results.items()},'lifecycle_status':stages,'seconds':times,'db':str(work.path)}
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8');print(json.dumps(report,ensure_ascii=False))
if __name__=='__main__':main()
