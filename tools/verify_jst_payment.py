"""Create a new synthetic directory, then run the source or packaged direct-payment UI."""
import argparse,json,os,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/p) for p in ('src','tests','tools')]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True,help='New directory only; all databases/files are synthetic')
    parser.add_argument('--exe',help='Packaged application; omit to run current source')
    parser.add_argument('--workflow',action='store_true',help='Exercise allocation order, selected locking and batch import layout instead')
    args=parser.parse_args();out=Path(args.output).resolve()
    if out.exists():raise FileExistsError(out)
    import jst_qa,jst_orders as orders,order_lifecycle as lc,warehouse_catalog
    path=jst_qa.fixture(out,2)
    for wh,c in warehouse_catalog.Repository(path.parent).catalogs().items():
        if args.workflow:
            # Explicitly empty tags in this synthetic execution fixture. Missing
            # tag-field blocking remains a business guard, never disabled here.
            for row in c['items'].values():row['商品标签']=''
            c['revision']=warehouse_catalog.revision(c['items']);warehouse_catalog.Repository(path.parent).save(c)
        orders.commit_refresh(path,orders.prepare_refresh(path,wh,catalog=c))
    receipt=orders.commit(path,orders.prepare(path,lc.current_targets(path)))
    if len(receipt['generated'])!=4:raise ValueError(receipt)
    env={**os.environ,'REPLENISHMENT_DATA_DIR':str(out/'settings'),'PYTHONUTF8':'1','QT_QPA_PLATFORM':'windows'}
    command=[str(Path(args.exe).resolve())] if args.exe else [sys.executable,str(ROOT/'src/app.py')]
    with (out/'process.log').open('wb') as log:
        mode='--workflow-refinements-smoke' if args.workflow else '--jst-payment-smoke'
        result=subprocess.run([*command,mode,str(out)],env=env,cwd=ROOT,stdout=log,stderr=log)
    report=json.loads((out/'report.json').read_text('utf-8'))
    print(json.dumps({'passed':report['passed'],'checks':len(report['checks']),'output':str(out),'exit_code':result.returncode},ensure_ascii=False))
    return 0 if result.returncode==0 and report['passed'] else 1


if __name__=='__main__':sys.exit(main())
