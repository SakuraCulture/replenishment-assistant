"""Replay existing user cases in a new isolated copy; never writes source data."""
import argparse,hashlib,json,shutil,sqlite3,sys,zipfile
from pathlib import Path
from contextlib import closing
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
import payment_matching as pm,lifecycle_batch as batch,order_lifecycle as lc

def sha(p):
    with Path(p).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--samples',type=Path,required=True);parser.add_argument('--snapshot',type=Path,required=True);parser.add_argument('--out',type=Path,required=True);a=parser.parse_args()
    out=a.out.resolve();out.relative_to(ROOT/'qa');out.mkdir(parents=True,exist_ok=False)
    settings=out/'settings';settings.mkdir();dbpath=settings/'workbench.sqlite3'
    snapshot_hash=sha(a.snapshot);shutil.copyfile(a.snapshot,dbpath)
    # These are isolated test states. Signed orders, returned facts, allocation
    # quantities and original frozen input remain unchanged.
    with closing(sqlite3.connect(dbpath)) as db,db:
        db.execute("DELETE FROM lifecycle_versions WHERE stage IN ('payment','receipt')")
        db.execute("DELETE FROM lifecycle_mappings WHERE name LIKE 'payment_match.%'")
    originals=sorted(p for p in (a.samples/'付款文件').glob('*.xlsx') if p.name[:3] in {f'{i:02}_' for i in range(1,8)});assert len(originals)==7
    hashes={p.name:sha(p) for p in originals}
    original03=next(p for p in originals if p.name.startswith('03_'))
    with zipfile.ZipFile(a.samples/'付款自动认单_7份案例与测试说明.zip') as z:
        member='付款文件/'+original03.name
        unmarked=out/'03_原始未填签发号.xlsx';unmarked.write_bytes(z.read(member))
    checks=[]
    def read(file):
        r=batch.read([file],'payment');assert not r['issues'],r['issues'];assert len(r['documents'])==1;return r['documents'][0]
    before=pm.build(dbpath);heads=before['heads']
    sd=next(h for h in heads if h['store']=='TBBLHDYJ20260617' and h['warehouse']=='山东仓');hb=next(h for h in heads if h['store']==sd['store'] and h['warehouse']=='河北仓')
    for i,file in enumerate(originals,1):
        if i==3:file=unmarked
        doc=read(file);index=pm.build(dbpath);r=pm.analyze(dbpath,doc,index)
        assert r['automatic']==(i<=5),(i,r)
        if i<=5:
            expected=(hb if i==2 else sd)['number'];assert r['candidates'][0]['number']==expected
            plan=pm.prepare(dbpath,[doc],None,index,None);saved=lc.commit(dbpath,plan)
            if i==1:
                repeat=lc.commit(dbpath,pm.prepare(dbpath,[doc],None,pm.build(dbpath),None));assert not repeat[0]['changed'];checks.append({'case':'01重复同一文件','passed':True,'new_version':False})
            if i==5:assert r['proof']=='previous_source' and not r['exact'] and '+0.01' in r['reason']
        else:
            assert '少 1 款' in r['reason'],r
            if i==7:assert 'CASE490-NOT-A-REAL-SKU' in r['reason'],r
            saved=[]
        checks.append({'case':i,'file':file.name,'passed':True,'automatic':r['automatic'],'state':r['state'],'reason':r['reason'],'saved_in_isolated_database':bool(saved)})
    edited=read(original03);r=pm.analyze(dbpath,edited,pm.build(dbpath))
    assert not r['automatic'] and r['prevent_rebind'],r
    # It may now also conflict with the local source relation saved by case 03.
    assert '冲突' in r['state'],r
    checks.append({'case':'03用户追加的错误签发号','passed':True,'state':r['state'],'reason':r['reason'],'saved':False})
    for p in originals:assert sha(p)==hashes[p.name]
    assert sha(a.snapshot)==snapshot_hash
    final=pm.build(dbpath)
    assert [(h['oid'],h['revision'],h['digest'],h['generation_id'],h['generation_digest']) for h in heads]==[(h['oid'],h['revision'],h['digest'],h['generation_id'],h['generation_digest']) for h in final['heads']]
    report={'passed':True,'checks':checks,'source_files_sha256':hashes,'source_files_unchanged':True,'snapshot_unchanged':True,'source_database_written':False,'platform_called':False,'company_selection_required':False,'manual_order_selection_required_for_cases_01_to_05':False,'note':'03正向案例使用此前交付ZIP中的未填号原件；用户追加错误单号版本单独验证为冲突。付款仅保存在新隔离库；不推断来源企业。'}
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8');print(json.dumps(report,ensure_ascii=False))

if __name__=='__main__':main()
