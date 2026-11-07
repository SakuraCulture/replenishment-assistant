"""Recalculate a read-only snapshot copy; never write to the source settings."""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))

def sha(path):
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()

def save(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n','utf-8')

def main(source_settings,out):
    from stockout_repository import Repository
    from stockout_lifecycle import preview,recalculate
    import stockout_metrics as metrics
    source_settings=source_settings.resolve();out=out.resolve()
    out.relative_to(ROOT/'qa')
    if out.exists():raise FileExistsError(out)
    source=source_settings/'analysis/stockout/stockout.sqlite3'
    source_hash=sha(source)
    out.mkdir(parents=True);settings=out/'settings';repo=Repository(settings)
    repo.folder.mkdir(parents=True)
    with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)) as original,closing(sqlite3.connect(repo.path)) as copy:
        original.execute('PRAGMA query_only=ON');original.backup(copy)
    copied=[]
    with repo.connect(write=True) as db:
        for item in list(db.execute('SELECT sha,path FROM originals')):
            path=Path(item['path']).resolve();path.relative_to(source.parent/'originals')
            assert sha(path)==item['sha']
            target=repo.folder/'originals'/path.name;target.parent.mkdir(exist_ok=True)
            shutil.copy2(path,target);assert sha(target)==item['sha']
            db.execute('UPDATE originals SET path=? WHERE sha=?',(str(target),item['sha']))
            copied.append({'source':str(path),'sha256':item['sha']})
        days=[r[0] for r in db.execute('SELECT day FROM current_days ORDER BY day')]
        runs=[r[0] for r in db.execute('SELECT DISTINCT p.run FROM current_days d JOIN publications p ON p.id=d.publication')]
        assert days and runs
        snapshot_ids=[r[0] for r in db.execute('SELECT id FROM store_snapshots')]
    q={'start':min(days),'end':max(days),'view':'all'}
    filtered={**q,'exclude_stopped':True,'exclude_disabled':True}
    before=metrics.query(repo,q);before_filtered=metrics.query(repo,filtered)
    old_settings=repo.settings()
    def snapshot_signature():
        digest=hashlib.sha256()
        with repo.connect() as db:
            for sid in snapshot_ids:
                for table,order in [('fact_records','sku'),('summaries','view'),('dimension_summaries','dimension,replen_status,inbound_status')]:
                    for row in db.execute('SELECT * FROM '+table+' WHERE snapshot=? ORDER BY '+order,(sid,)):
                        digest.update(repr(tuple(row)).encode('utf-8'))
        return digest.hexdigest()
    old_signature=snapshot_signature();expected={}
    with repo.connect() as db:
        for label,options in [('all',q),('default_status_exclusions',filtered)]:
            condition,args=metrics.where(options);ex=metrics.excluded_sql(options)
            expected[label]=[dict(r) for r in db.execute(f'''SELECT s.store,s.name,
              sum(f.month>0 AND NOT ({ex})) base,
              sum(f.month>0 AND f.stock IS NOT NULL AND f.stock<=0 AND NOT ({ex})) zero,
              sum(f.month>0 AND f.stock<0 AND NOT ({ex})) negative,
              sum(f.month>0 AND ({ex})) removed_base
              FROM {metrics.LINKS} JOIN facts f ON f.snapshot=s.id WHERE {condition}
              GROUP BY s.store,s.name ORDER BY s.store''',args)]
    elapsed=time.monotonic()
    for rid in runs:
        result=recalculate(repo,preview(repo,rid,'recalculate','original'),progress=lambda msg:print(msg,flush=True))
        assert result['state'] in {'COMPLETE','PARTIAL'}
    elapsed=time.monotonic()-elapsed
    after=metrics.query(repo,q);after_filtered=metrics.query(repo,filtered);store_cuts={}
    for label,options in [('all',q),('default_status_exclusions',filtered)]:
        actual=metrics.cuts(repo,options,('store',))['items'];store_cuts[label]=actual
        observed={r['store']:r for r in actual}
        for row in expected[label]:
            for key in ('base','zero','removed_base'):assert observed[row['store']][key]==row[key],(label,row,observed[row['store']])
    assert before['needs_negative_stock_recalculation']
    assert not after['needs_negative_stock_recalculation'] and after['total']['bad']==0
    assert snapshot_signature()==old_signature and repo.settings()==old_settings
    source_unchanged=sha(source)==source_hash and all(sha(Path(r['source']))==r['sha256'] for r in copied)
    assert source_unchanged
    result={'passed':True,'source_database_sha256':source_hash,'source_unchanged':source_unchanged,
            'old_snapshot_signature':old_signature,'old_snapshots_unchanged':True,'settings_unchanged':True,
            'before':before,'before_default_status_exclusions':before_filtered,'after':after,
            'after_default_status_exclusions':after_filtered,'expected_from_raw':expected,'store_cuts':store_cuts,
            'seconds':elapsed,'copied_originals':len(copied),'source_snapshots':len(snapshot_ids),
            'scope':'Frozen read-only source backup; only QA copy and copied originals used by the actual offline recalculate path. No platform calls; no live settings modified.'}
    save(out/'report.json',result)
    print(json.dumps({k:result[k] for k in ('passed','seconds','source_unchanged')},ensure_ascii=False),flush=True)
    print(json.dumps({'all':after['total'],'default_status_exclusions':after_filtered['total']},ensure_ascii=False),flush=True)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--source-settings',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();main(args.source_settings,args.output)
