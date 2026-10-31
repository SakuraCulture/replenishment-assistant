"""Strict frozen archive export comparison, including row order and Excel styles."""
import argparse,hashlib,json,subprocess,sys,threading
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def capture(source,db,out):
    sys.path[:0]=[str(source/'src'),str(ROOT/'tools')]
    import archive_batch as ab,history_read as hr,normal_exports,history_forms
    from history_read import repository
    from structure_oracle import excel_snapshot
    out.mkdir(parents=True,exist_ok=False)
    heads=hr.execute(db,out/'index.db',{'kind':'list'},threading.Event(),lambda _:None)['records']
    manifests={}
    for kind in ('calculation','signed','reference','original'):
        for mode in ('separate','combined'):
            plan=ab.prepare(db,heads,kind,mode);receipt=ab.execute(db,plan,out)
            if any(receipt[k] for k in ('failed','skipped','cancelled')):raise AssertionError(receipt)
            values=[]
            for f in receipt['files']:
                path=Path(f['path']);values.append({'filename':path.name,'value':sha(path) if kind=='original' else excel_snapshot(path)})
            manifests[kind+'/'+mode]=values
    repo=repository(db)
    for h in heads:
        order=repo.get(h['day'],h['store'],h['revision']);p=out/(h['store']+'-'+h['warehouse']+'-merchant.xlsx');normal_exports.export(order,p,warehouse=h['warehouse']);manifests['merchant/'+h['store']+'/'+h['warehouse']]=excel_snapshot(p)
    (out/'snapshot.json').write_text(json.dumps(manifests,ensure_ascii=False,default=str),encoding='utf-8')


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--baseline',type=Path,required=True);ap.add_argument('--output',type=Path,required=True);ap.add_argument('--db',type=Path);ap.add_argument('--source',type=Path);a=ap.parse_args()
    if a.source:return capture(a.source.resolve(),a.db.resolve(),a.output.resolve())
    out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);sys.path[:0]=[str(ROOT/'tests'),str(ROOT/'src'),str(ROOT/'tools')]
    from test_archive_batch import fixture
    fixture_dir=out/'fixture';fixture_dir.mkdir();work,repo,heads,catalogs=fixture(fixture_dir);before=sha(work.path)
    for name,src in [('old',a.baseline.resolve()),('new',ROOT)]:
        subprocess.run([sys.executable,__file__,'--baseline',str(a.baseline),'--source',str(src),'--db',str(work.path),'--output',str(out/name)],check=True)
    from parallel_qa import diff
    values=[json.loads((out/name/'snapshot.json').read_text(encoding='utf-8')) for name in ('old','new')];changes=diff(*values)
    report={'passed':not changes and sha(work.path)==before,'differences':changes,'cases':list(values[0]),'database_unchanged':sha(work.path)==before,'comparison':'exact ordered values/types/style snapshot; original Excel SHA256; same frozen archive database; explicit standard clock/build cell whitelist only'}
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(report,ensure_ascii=True))
    if not report['passed']:raise AssertionError('archive exports differ')

if __name__=='__main__':main()
