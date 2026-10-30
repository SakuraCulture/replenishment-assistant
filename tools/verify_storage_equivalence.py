"""Independent 0.7.4/storage-v2 logical persistence + workbook oracle; physical encoding intentionally differs."""
import argparse,json,os,pickle,subprocess,sys,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]


def capture(source,fixture,out):
    out.mkdir(parents=True,exist_ok=False);sys.path[:0]=[str(source/'src'),str(ROOT/'tools')]
    import copy,sqlite3,uuid,work_repository as wr,normal_orders as no,normal_delivery as nd,normal_exports as ne
    from structure_oracle import excel_snapshot
    p=wr.loads(fixture.read_text(encoding='utf-8'));p['day']=wr.today();no.MODEL_VERSION=p['model_version'];tick=[0]
    def uid():tick[0]+=1;return uuid.UUID(int=tick[0])
    uuid.uuid4=uid;wr.now=lambda:p['day']+'T12:00:00+08:00'
    work=wr.Repository(out/'settings');repo=no.Repository(work);codes=list(p['results']);records=[]
    def snapshot(label):
        with work.connect() as db:
            tables=[r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            sql={}
            for t in tables:
                if t in ('snapshot_chunks','snapshot_storage_meta'):continue
                values=[]
                for row in db.execute('SELECT * FROM '+t+' ORDER BY rowid'):
                    row=list(row)
                    if t in ('jobs','normal_revisions','normal_work_revisions'):
                        if (source/'src/storage_chunks.py').exists():
                            import storage_chunks
                            row[-1]=storage_chunks.load(row[-1],db)
                        else:
                            import zlib
                            row[-1]=wr.loads(zlib.decompress(row[-1]))
                    values.append(row)
                sql[t]=values
        records.append({'label':label,'job':work.get_job(p['day']),'orders':[repo.get(p['day'],c) for c in codes],'events':repo.events(),'sql':sql})
    saved=work.save_job(p['day'],p);snapshot('save')
    same=work.save_job(p['day'],p,saved['revision']);assert same==saved;snapshot('unchanged_save')
    repo.confirm(p,codes,saved['revision'],{c:None for c in codes});snapshot('first_sign')
    repo.confirm(p,codes,work.get_job(p['day'])['revision'],{c:1 for c in codes});snapshot('unchanged_sign')
    p['results'][codes[0]]['qa_audit']={'empty':None,'zero':0,'blank':'','boolean':False}
    repo.confirm(p,[codes[0]],work.get_job(p['day'])['revision'],{codes[0]:1});snapshot('partial_revision')
    # The archived result object is independently loaded; source payload untouched.
    for c in codes:
        order=repo.get(p['day'],c);path=out/ne.filename(order);ne.export(order,path)
    # Both old and current code validate the prepared order; execution must reject
    # an intervening revision rather than exporting stale or mixed content.
    batch=nd.prepare_batch(repo,p,[codes[0]])
    changed=copy.deepcopy(p);changed['results'][codes[0]]['qa_audit']['blank']='changed'
    repo.confirm(changed,[codes[0]],work.get_job(p['day'])['revision'],{codes[0]:2})
    destination=out/'race';destination.mkdir()
    try:nd.execute_batch(repo,batch,destination)
    except wr.Conflict:pass
    else:raise AssertionError('intervening order revision accepted')
    assert not list(destination.iterdir());snapshot('revision_race')
    workbooks={f.name:excel_snapshot(f) for f in out.glob('*.xlsx')}
    extras={}
    for f in out.glob('*.xlsx'):
        with zipfile.ZipFile(f) as z:extras[f.name]={n:z.read(n) for n in z.namelist() if n.startswith(('xl/comments/','xl/drawings/')) or n=='docProps/custom.xml'}
    from datetime import date,datetime
    original={'rows':[{'E':'00001','D':'类型边界','value':0,'empty':None,'text':'','nested':{'a':1,'b':2}}]}
    variants=[]
    for value in (False,0.0,None,'',1,[],(),date(2026,10,30),datetime(2026,10,30,12,30)):
        other=copy.deepcopy(original);other['rows'][0]['value']=value;variants.append(no.revision_diff(original,other))
    other=copy.deepcopy(original);del other['rows'][0]['empty'];other['rows'][0]['new_field']=None;variants.append(no.revision_diff(original,other))
    other=copy.deepcopy(original);other['rows'][0]['nested']={'b':2,'a':1};variants.append(no.revision_diff(original,other))
    (out/'capture.pkl').write_bytes(pickle.dumps({'records':records,'workbooks':workbooks,'extras':extras,'revision_type_cases':variants},protocol=5))


def main():
    p=argparse.ArgumentParser();p.add_argument('--source',type=Path);p.add_argument('--fixture',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    if a.source:return capture(a.source.resolve(),a.fixture.resolve(),a.output.resolve())
    out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);env=os.environ.copy();env['PYTHONHASHSEED']='0'
    for name,root in [('old',ROOT/'qa/storage_080/baseline'),('new',ROOT)]:
        subprocess.run([sys.executable,__file__,'--source',str(root),'--fixture',str(a.fixture.resolve()),'--output',str(out/name)],env=env,check=True)
    sys.path.insert(0,str(ROOT/'tools'));from parallel_qa import diff
    old=pickle.loads((out/'old/capture.pkl').read_bytes());new=pickle.loads((out/'new/capture.pkl').read_bytes());d=diff(old,new)
    report={'passed':not d,'differences':d,'baseline':'39f4944','scenarios':['save','unchanged_save','first_sign','unchanged_sign','partial_revision','revision_race'],'scope':'complete decoded daily/archive/work-revision payloads, all other original SQL columns, events, headers, IDs; merchant workbook values/types/row/column/sheet order/styles/comments/custom properties','physical_storage_exclusions':['snapshot_chunks table','snapshot_storage_meta table (backup path/format metadata)','three payload columns decoded with each version codec before comparison'], 'business_field_whitelist':[],'determinism':'UUID generator and wall-clock frozen identically; same frozen calculation payload; no value tolerances or removed fields'}
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(report,ensure_ascii=False));assert not d

if __name__=='__main__':main()
