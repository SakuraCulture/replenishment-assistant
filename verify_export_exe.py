"""Launch isolated actual EXE, generating five labelled synthetic store inputs."""
import argparse,json,os,shutil,subprocess,hashlib,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parent;sys.path.insert(0,str(ROOT/'src'))
from verify_batch import write_fixture
import input_adapter
p=argparse.ArgumentParser();p.add_argument('--exe',type=Path);p.add_argument('--source',action='store_true');p.add_argument('--fixtures',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);fixture=out/'fixtures';fixture.mkdir()
rows,meta=input_adapter.read_table(a.fixtures/'sales.xlsx',None,['商品编码']);one=[r for r in rows if r['仓库/门店编码']=='QA_A'];five=[]
for i in range(5):
    for r in one:five.append({**r,'仓库/门店编码':'QA_'+str(i),'仓库/门店名称':'合成验收店'+str(i)})
write_fixture(fixture/'five.xlsx',meta['headers'],five)
for name in ['河北仓.xlsx','山东仓.xlsx']:shutil.copy2(a.fixtures/name,fixture/name)
shutil.copy2(ROOT/'qa/normal_070/rc3-candidate/real_None_True.pklz',fixture/'real.pklz')
env=os.environ.copy()
for key in ['PYTHONPATH','PYTHONHOME','QT_QPA_PLATFORM','QT_PLUGIN_PATH']:env.pop(key,None)
env['PATH']=str(Path(env['WINDIR'])/'System32')+';'+env['WINDIR'];env['REPLENISHMENT_DATA_DIR']=str(out/'settings')
command=[sys.executable,str(ROOT/'src/app.py')] if a.source else [str(a.exe.resolve())]
run=subprocess.run(command+['--delivery-smoke',str(fixture),str(out)],env=env,cwd=out,timeout=300);assert run.returncode==0,run.returncode
path=out/'export_ui_report.json';r=json.loads(path.read_text(encoding='utf-8'));assert r['passed'],r;r['development']=a.source;r['exe_sha256']=None if a.source else hashlib.sha256(a.exe.read_bytes()).hexdigest();path.write_text(json.dumps(r,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(r,ensure_ascii=False))
