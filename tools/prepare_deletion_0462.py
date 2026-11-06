"""Freeze 0.46.1 and generate an explicitly synthetic native-format QA database.

Only new directories under qa/deletion_0462 are written. No production profiles.
"""
import argparse,copy,hashlib,json,random,sqlite3,subprocess,sys,zipfile
from contextlib import closing
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];QA=ROOT/'qa/deletion_0462'
BASE='8795b78dd76869c652bb45916c86df6bd9f67e4f'
def sha(path):
    with Path(path).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def save(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
def freeze():
    QA.mkdir(exist_ok=False);archive=QA/'baseline.zip'
    subprocess.run(['git','archive','-o',str(archive),BASE,'src','tests','tools'],cwd=ROOT,check=True)
    with zipfile.ZipFile(archive) as z:z.extractall(QA/'baseline')
    previous=json.loads((ROOT/'releases/0.46.1/rc2/frozen.json').read_text('utf-8-sig'))
    protected=previous['protected']
    for name in ('补货助手/补货助手.exe','补货助手_0.46.1_rc2_当前格式与采集修复.zip'):
        path=ROOT/'交付_0.46.1_rc2'/name;protected[str(path)]=sha(path)
    assert all(sha(p)==h for p,h in protected.items())
    save(QA/'frozen.json',{'commit':BASE,'archive_sha256':sha(archive),'protected':protected})
    save(QA/'inputs.json',{'derived':str(ROOT/'qa/current_storage_0461/current-fixture'),
                          'derived_warm':str(ROOT/'qa/current_storage_0461_rc2/warm.sqlite3'),
                          'small':str(ROOT/'qa/current_storage_0461/synthetic/settings'),
                          'native':str(QA/'native/settings'),'native_warm':str(QA/'native-warm.sqlite3')})
    print('Frozen '+BASE,flush=True)
def native():
    sys.path[:0]=[str(QA/'baseline/src'),str(QA/'baseline/tests')]
    import test_archive_orders as f,work_repository as wr,normal_orders as no,storage_chunks as c
    out=QA/'native';out.mkdir(exist_ok=False);work,repo,signed=f.seed(out)
    rng=random.Random(462);days=['2026-10-20','2026-10-21','2026-10-22'];codes=[f'S{i:02d}' for i in range(8)]
    spec={'synthetic':True,'format':'native v2 via immutable 0.46.1 writer, no legacy conversion',
          'days':days,'stores_per_day':len(codes),'rows_per_store':12000,'random_bytes_per_row':1200,
          'purpose':'Large native chunk layout; random trace fields inflate history and are not business observations'}
    with closing(work.connect()) as db,db:
        for table in ('normal_orders','normal_revisions','normal_work_revisions'):db.execute('DELETE FROM '+table)
        for day in days:
            rows=[({'B':code,'E':str(i),'K':float(i%17)},
                   {'source_row':i+2,'raw_trace':rng.randbytes(spec['random_bytes_per_row']).hex(),
                    'blank':'','none':None,'flag':False})
                  for code in codes for i in range(spec['rows_per_store'])]
            states={s:{'name':s,'state':'待计算'} for s in codes}
            payload={'schema':1,'day':day,'model_version':'0.3.4-rules','states':states,'results':{},'input_dirty':True,
                     'loaded':{'stores':{s:s for s in codes},'normalized':rows,
                               'warehouse_rows':{'河北仓':rows,'山东仓':rows}},
                     'export_log':[],'pending_copy':None,'store_ui':{s:{'flag':False} for s in codes}}
            pack=c.prepare(payload);blob=c.put(db,pack);revision='native-'+day
            db.execute('INSERT INTO normal_work_revisions VALUES(?,?,?)',(revision,day,blob))
            db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?)',(day,'draft-'+day,pack.digest,wr.now(),wr.dumps(wr.summary(payload)),blob))
            for code in codes:
                p=copy.deepcopy(signed);p['work_revision']=revision;p['result']['store']=code;p['result']['store_name']=code
                for row in p['result']['rows']:row['B']=code
                oid=day+'-'+code;digest=no.fingerprint(p['result'])
                db.execute('INSERT INTO normal_orders VALUES(?,?,?,?,?,?)',(day,code,'normal_model',oid,'N-'+oid,1))
                db.execute('INSERT INTO normal_revisions VALUES(?,?,?,?,?)',(oid,1,digest,wr.now(),c.put(db,c.prepare(p,binding=digest))))
            print(json.dumps({'day':day,'logical_mib':pack.size/2**20,'new_blocks':len(pack.chunks)},ensure_ascii=False),flush=True)
            del rows,payload,pack
        spec['chunks']=db.execute('SELECT COUNT(*) FROM snapshot_chunks').fetchone()[0]
        spec['compressed_bytes']=db.execute('SELECT SUM(length(data)) FROM snapshot_chunks').fetchone()[0]
    spec['database_bytes']=work.path.stat().st_size;spec['sha256']=sha(work.path)
    save(QA/'native-fixture.json',spec);print(json.dumps(spec,ensure_ascii=False),flush=True)
def warm():
    sys.path.insert(0,str(QA/'baseline/src'))
    import maintenance_storage as ms
    source=QA/'native/settings/workbench.sqlite3';target=QA/'native-warm.sqlite3'
    if target.exists():raise FileExistsError(target)
    with closing(sqlite3.connect(source.resolve().as_uri()+'?mode=ro',uri=True)) as db,closing(sqlite3.connect(target)) as cache,cache:
        ms.cache_schema(cache)
        for i,(h,size,data) in enumerate(db.execute('SELECT hash,size,data FROM snapshot_chunks'),1):
            ms.references_for(cache,h,size,data)
        save(QA/'native-cache.json',{'chunks':i,'source_sha256':sha(source),'scope':'Full baseline reference verification outside timing'})
    print('Native cache ready',flush=True)
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('mode',choices=['freeze','native','warm']);a=p.parse_args();globals()[a.mode]()
