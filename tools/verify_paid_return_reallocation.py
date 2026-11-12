"""Build an owned paid-return fixture and exercise the native application."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/p) for p in ('src','tests')]


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--out',required=True,type=Path)
    parser.add_argument('--exe',type=Path)
    args=parser.parse_args();out=args.out.resolve();out.relative_to(ROOT/'qa')
    out.mkdir(parents=True,exist_ok=False);(out/'exports').mkdir()
    import test_jst as fixture
    import jst_batches as batches
    import jst_orders as orders
    import order_lifecycle as lifecycle
    import purchases
    import warehouse_catalog
    import export_settings
    from contextlib import closing
    import sqlite3
    f=fixture.Service();f.setUp()
    try:
        hb=f.h;sd=next(h for h in purchases.targets(f.path) if h['store']==hb['store'] and h['warehouse']=='山东仓');peer=f.second_store()
        def save(h,qty,sku):
            previous=f.f.h;f.f.h=h
            try:f.f.save('return',[{'门店编码':h['store'],'供货仓':h['warehouse'],'商品编码':sku,'门店确认数量':qty}])
            finally:f.f.h=previous
        save(sd,20,'0002');catalog=fixture.catalog('山东仓',stock=70)
        item=catalog['items'].pop('0001');item['商品编码']='0002';catalog['items']['0002']=item;catalog['revision']=warehouse_catalog.revision(catalog['items'])
        p=batches.preview_new(f.path,[hb,sd,peer],{'河北仓':fixture.catalog(),'山东仓':catalog});r=batches.execute(f.path,p)
        if r['errors']:raise AssertionError(r)
        with closing(sqlite3.connect(f.path)) as db:ids={h['warehouse']:orders.active(db,h) for h in (hb,sd)};peer_gid=orders.active(db,peer)
        for gid in ids.values():lifecycle.commit(f.path,orders.payment_plan(f.path,gid))
        save(hb,30,'0001');save(sd,15,'0002')
        with closing(sqlite3.connect(f.path)) as db:old_payments={h['warehouse']:lifecycle.current(db,h,'payment') for h in (hb,sd)}
        data={'heads':[hb,sd],'batch_id':r['batch_id'],'old_ids':ids,'old_payments':old_payments,
              'old_generations':{wh:orders.get(f.path,gid) for wh,gid in ids.items()},'peer':peer,'peer_gid':peer_gid,
              'peer_generation':orders.get(f.path,peer_gid),'expected':{'河北仓':'30','山东仓':'15'},'db_name':Path(f.path).name}
        settings=out/'settings';shutil.copytree(Path(f.path).parent,settings)
        export_settings.save(settings/export_settings.FILENAME,str(out/'exports'))
        (out/'fixture.json').write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
        (out/'synthetic.json').write_text(json.dumps({'synthetic':True,'live_platform':False}),encoding='utf-8')
    finally:f.tearDown()
    command=[str(args.exe.resolve())] if args.exe else [sys.executable,str(ROOT/'src/app.py')]
    env={**os.environ,'REPLENISHMENT_DATA_DIR':str(settings),'LOCALAPPDATA':str(out/'localappdata'),'QT_QPA_PLATFORM':'windows','PYTHONUTF8':'1'}
    with (out/'process.log').open('wb') as log:
        process=subprocess.Popen(command+['--paid-return-reallocation-smoke',str(out)],cwd=ROOT,env=env,stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW)
        start=time.monotonic()
        while process.poll() is None:
            if time.monotonic()-start>180:raise TimeoutError(f'Owned QA process {process.pid} retained; no forced termination')
            time.sleep(.2)
    report=json.loads((out/'report.json').read_text('utf-8'));report['process_exit']=process.returncode
    if args.exe:report['exe_sha256']=hashlib.sha256(args.exe.read_bytes()).hexdigest()
    (out/'acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'passed':report['passed'],'checks':len(report['checks']),'exit_code':process.returncode,'out':str(out)},ensure_ascii=False))
    return 0 if report['passed'] and process.returncode==0 else 1


if __name__=='__main__':raise SystemExit(main())
