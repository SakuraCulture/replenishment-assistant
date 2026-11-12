"""Fresh source/EXE GUI acceptance. Only writes a new qa output directory."""
import argparse,json,os,sys,subprocess,time,hashlib
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--out',type=Path,required=True);parser.add_argument('--exe',type=Path);args=parser.parse_args()
    out=args.out.resolve();out.relative_to(ROOT/'qa');out.mkdir(parents=True,exist_ok=False);(out/'inputs').mkdir();(out/'exports').mkdir()
    from test_stockout_analysis import workbook
    from stockout_repository import Repository
    from stockout_common import file_hash,today
    settings=out/'settings';repo=Repository(settings);repo.initialize();repo.save_settings({'stores':[{'code':'S','name':'测试门店S'}],'routes':{'S':{'primary':'山东仓','secondary':None}},'profile':None})
    heads=['商品编码','分类','基本售价','出库箱规','商品状态','可用数','虚拟分类','商品标签','供应商名称']
    rows=[['A','食品',1,1,'启用',0,'','','供货一'],['B','食品',1,1,'启用',0,'淘便考核','','供货一'],['C','饮品',1,1,'启用',3,'','','供货二'],['D','饮品',1,1,'禁用',0,'淘便考核','日配','供货二']]
    catalogs={}
    for wh in ('山东仓','河北仓'):
        path=workbook(out/'inputs'/(wh+'.xlsx'),heads,rows);catalogs[wh]={'file':str(path),'sha256':file_hash(path),'rows':4}
    path=workbook(out/'inputs'/'store.xlsx',['仓库/门店编码','商品编码','商品名称','月销','可用库存','在途库存'],[['S',r[0],r[0],1,r[5],0] for r in rows])
    fixture={'day':today(),'catalogs':catalogs,'files':[{'store':'S','file':str(path),'sha256':file_hash(path),'rows':4,'observed':today()+'T08:00:00+08:00'}]}
    (out/'fixture.json').write_text(json.dumps(fixture,ensure_ascii=False),'utf-8');(out/'synthetic.json').write_text('{"synthetic":true,"live_platform":false}','utf-8')
    command=([str(args.exe.resolve())] if args.exe else [sys.executable,str(ROOT/'src/app.py')])+['--stockout-hub-smoke',str(out)]
    env={**os.environ,'REPLENISHMENT_DATA_DIR':str(settings),'LOCALAPPDATA':str(out/'localappdata'),'QT_QPA_PLATFORM':'windows','PYTHONUTF8':'1'}
    with (out/'process.log').open('wb') as log:
        process=subprocess.Popen(command,cwd=ROOT,env=env,stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW);begin=time.monotonic()
        while process.poll() is None:
            if time.monotonic()-begin>240:raise TimeoutError(f'Owned QA process {process.pid} retained; not force-killed')
            time.sleep(.2)
    result=json.loads((out/'report.json').read_text('utf-8'));result['exit_code']=process.returncode
    if args.exe:result['exe_sha256']=hashlib.sha256(args.exe.read_bytes()).hexdigest()
    (out/'acceptance.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),'utf-8')
    print(json.dumps({'passed':result['passed'],'checks':len(result['checks']),'out':str(out)},ensure_ascii=False));return 0 if result['passed'] and not process.returncode else 1


if __name__=='__main__':raise SystemExit(main())
