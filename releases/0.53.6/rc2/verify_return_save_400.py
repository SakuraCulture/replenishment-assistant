"""Build marked synthetic return/operations files and drive a real packaged UI."""
import argparse,copy,hashlib,json,os,sqlite3,subprocess,sys,time
from contextlib import closing
from pathlib import Path
ROOT=Path('C:\\Users\\lenovo\\.codex\\worktrees\\data-location-0521\\replenishment_mvp_20261029')
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]


def fixture(out):
    from test_archive_orders import seed
    import openpyxl,normal_orders as no,order_lifecycle as lc,work_repository as wr
    import operations_flow as flow,result_semantics as sem,storage_chunks
    work,repo,p=seed(out);rows=[]
    for wh in ('河北仓','山东仓'):
        template=next(r for r in p['result']['rows'] if r['AU']==wh)
        for i in range(400):
            r=copy.deepcopy(template);r.update(E=('0001' if wh=='河北仓' else '0002')+f'{i:05d}',D='隔离回传商品'+str(i));r['_meta']['export_row']=len(rows)+2;rows.append(r)
    p['result']['rows']=rows;p['result']['summary']=sem.summarize(rows)
    with closing(work.connect()) as db,db:
        db.execute('UPDATE normal_revisions SET payload=?,digest=?',(no.encode(p,db),no.fingerprint(p['result'])))
        flow.schema(db);db.execute('INSERT INTO lifecycle_routes VALUES(?,?,2)',('oid',1))
    storage_chunks.backup_before_write(work.path)
    heads=lc.current_targets(work.path);(out/'inputs').mkdir()
    import lifecycle_io as io
    for stage in ('return','operations'):
        book=openpyxl.Workbook();sheet=book.active;sheet.title='业务明细';headers=io.fields(stage);sheet.append(headers)
        for h in heads:
            for i,sku in enumerate(h['products']):
                if i==399:continue  # Explicitly exercise missing -> 0.
                r={'补货单号':h['number'],'门店编码':h['store'],'供货仓':h['warehouse'],'商品编码':sku,'商品名称':'隔离回传商品'+str(i),
                   '数量单位':'库存单位',io.QUANTITIES[stage]:None if i%7==0 else 0 if i%5==0 else 5 if stage=='return' else 7,
                   io.DOCNUM[stage]:'SYN-'+stage,io.DOCDATE[stage]:wr.today()}
                sheet.append([r.get(k) for k in headers])
                if i==1:sheet.append([r.get(k) for k in headers])  # Same-value duplicate.
            r.update(商品编码='EXTRA-'+h['warehouse'],商品名称='明确核对的清单外合成商品');r[io.QUANTITIES[stage]]=3
            sheet.append([r.get(k) for k in headers])
        book.save(out/'inputs'/(stage+'.xlsx'));book.close()
    (out/'fixture.json').write_text(json.dumps({'heads':heads,'rows_per_warehouse':401},ensure_ascii=False),'utf-8',newline='\n')
    (out/'synthetic.json').write_text(json.dumps({'synthetic':True,'live_platform':False,'purpose':'return-save-performance-acceptance'}),'utf-8',newline='\n')


def main():
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);p.add_argument('--exe',type=Path);p.add_argument('--timeout',type=int,default=180);a=p.parse_args()
    out=a.out.resolve();out.mkdir(parents=True,exist_ok=False);fixture(out)
    env=os.environ.copy()
    for k in ('PYTHONPATH','PYTHONHOME','QT_PLUGIN_PATH','REPLENISHMENT_DATA_DIR','REPLENISHMENT_PORTABLE_PATHS','QT_QPA_PLATFORM'):env.pop(k,None)
    env.update(PYTHONUTF8='1',REPLENISHMENT_DATA_DIR=str(out/'settings'),LOCALAPPDATA=str(out/'localappdata'),APPDATA=str(out/'roaming'))
    if not a.exe:env['QT_QPA_PLATFORM']='offscreen'
    command=[str(a.exe.resolve())] if a.exe else [sys.executable,str(ROOT/'src/app.py')]
    result={'passed':False,'synthetic':True,'live_platform':False,'runs':[],'exe_sha256':hashlib.sha256(a.exe.read_bytes()).hexdigest() if a.exe else None}
    for mode in ('exercise','reopen'):
        with (out/(mode+'-process.log')).open('wb') as log:
            process=subprocess.Popen(command+['--return-save-smoke',str(out),mode],cwd=out,env=env,stdout=log,stderr=log)
            try:code=process.wait(a.timeout)
            except subprocess.TimeoutExpired:
                result.update(error='Acceptance timed out; owned process retained without forced termination',pid=process.pid)
                (out/'report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),'utf-8');raise
        f=out/(mode+'-report.json');r=json.loads(f.read_text('utf-8')) if f.exists() else {'passed':False,'error':'No native report'}
        r['exit_code']=code;result['runs'].append(r);(out/'report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),'utf-8',newline='\n')
        if code or not r['passed']:raise AssertionError(r)
    result['passed']=True;(out/'report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),'utf-8',newline='\n')
    print(json.dumps({'passed':True,'checks':sum(len(r['checks']) for r in result['runs']),'report':str(out/'report.json')},ensure_ascii=False))


if __name__=='__main__':main()
