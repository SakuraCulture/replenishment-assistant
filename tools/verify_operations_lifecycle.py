"""Create isolated synthetic inputs, then drive the source or frozen Qt app."""
import argparse,copy,hashlib,json,os,sqlite3,subprocess,sys,time
from contextlib import closing
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--out',required=True,type=Path);parser.add_argument('--exe',type=Path);args=parser.parse_args()
    out=args.out.resolve();out.relative_to(ROOT/'qa');out.mkdir(parents=True,exist_ok=False);settings=out/'settings';settings.mkdir();(out/'inputs').mkdir();(out/'exports').mkdir()
    from test_archive_orders import seed
    import test_jst as fixture
    import work_repository as wr,warehouse_catalog as wc,operations_flow as flow
    import lifecycle_io as io,order_lifecycle as lc,normal_orders as no,purchases,source_archive
    import openpyxl
    work,repo,payload=seed(out);assert work.path.resolve()==settings/'workbench.sqlite3';heads=lc.current_targets(work.path);catalogs={}
    for wh,sku,step,stock in [('河北仓','0001',15,1079),('山东仓','0002',24,5000)]:
        c=fixture.catalog(wh,stock=stock,step=step,price='0.75');item=c['items'].pop('0001');item['商品编码']=sku;item['虚拟分类']='';item['供应商名称']='合成供货商';c['items'][sku]=item;c['revision']=wc.revision(c['items'])
        path=out/'inputs'/(wh+'-合成商品资料.xlsx');book=openpyxl.Workbook();sheet=book.active;sheet.title='商品资料';fields=wc.FIELDS+['商品标签','虚拟分类','供应商名称'];fields=list(dict.fromkeys(fields));sheet.append(fields);sheet.append([item.get(f) for f in fields]);book.save(path);book.close()
        c.update(source_path=str(path),source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),imported_at=wr.now())
        source_archive.capture(work.path,path,c['source_sha256']);wc.Repository(settings).save(c);catalogs[wh]=c
    with closing(sqlite3.connect(work.path)) as db,db:
        flow.schema(db)
        for h in heads:db.execute('INSERT OR IGNORE INTO lifecycle_routes VALUES(?,?,2)',(h['oid'],h['revision']))
        db.execute('UPDATE normal_work_revisions SET payload=?',(no.encode({'catalogs':catalogs},db),))
    for h in heads:
        path=out/'inputs'/(h['warehouse']+'-回传.xlsx');book=openpyxl.Workbook();sheet=book.active;fields=io.fields('return');sheet.append(fields)
        d={'补货单号':h['number'],'门店编码':h['store'],'供货仓':h['warehouse'],'商品编码':'0001' if h['warehouse']=='河北仓' else '0002','商品名称':'合成饮品','门店确认数量':1090 if h['warehouse']=='河北仓' else 800,'数量单位':'库存单位'}
        sheet.append([d.get(f) for f in fields]);book.save(path);book.close();info=io.inspect([path])[0];sheet_info=info['sheets'][0]
        scanned=io.scan('return',[{**info,**sheet_info,'mapping':{name:i for i,name in enumerate(sheet_info['headers'])}}]);lc.commit(work.path,lc.prepare_numbered(work.path,scanned,h))
    (out/'synthetic.json').write_text(json.dumps({'synthetic':True,'live_platform':False,'heads':heads},ensure_ascii=False),'utf-8')
    command=([str(args.exe.resolve())] if args.exe else [sys.executable,str(ROOT/'src/app.py')])+['--operations-lifecycle-smoke',str(out)]
    env={**os.environ,'REPLENISHMENT_DATA_DIR':str(settings),'LOCALAPPDATA':str(out/'localappdata'),'PYTHONUTF8':'1','QT_QPA_PLATFORM':'windows'}
    with (out/'process.log').open('wb') as log:
        process=subprocess.Popen(command,cwd=ROOT,env=env,stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW);start=time.monotonic()
        while process.poll() is None:
            if time.monotonic()-start>240:raise TimeoutError(f'Owned acceptance process {process.pid} retained; no forced kill')
            time.sleep(.2)
    result=json.loads((out/'report.json').read_text('utf-8'));result['exit_code']=process.returncode
    if args.exe:result['exe_sha256']=hashlib.sha256(args.exe.read_bytes()).hexdigest()
    (out/'acceptance.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),'utf-8')
    print(json.dumps({'passed':result['passed'],'checks':len(result['checks']),'out':str(out),'error':result.get('error')},ensure_ascii=False))
    return 0 if result['passed'] and not process.returncode else 1

if __name__=='__main__':raise SystemExit(main())
