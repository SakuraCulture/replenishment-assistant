"""Supervise copy-only maintenance through source or actual Windows EXE."""
import argparse,hashlib,json,os,subprocess,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'qa/export_071/qa-deps'))
import psutil

def main():
    p=argparse.ArgumentParser();p.add_argument('--exe',type=Path);p.add_argument('--fixture',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);env=os.environ.copy()
    for k in ['PYTHONPATH','PYTHONHOME','QT_PLUGIN_PATH','QT_QPA_PLATFORM']:env.pop(k,None)
    env['REPLENISHMENT_DATA_DIR']=str(out/'settings');env['PATH']=env['WINDIR']+';'+str(Path(env['WINDIR'])/'System32')
    if not a.exe:env['QT_QPA_PLATFORM']='offscreen'
    command=[str(a.exe.resolve())] if a.exe else [sys.executable,str(ROOT/'src/app.py')]
    with (out/'run.log').open('w',encoding='utf-8') as log:
        run=subprocess.Popen(command+['--storage-smoke',str(a.fixture.resolve()),str(out)],cwd=out,env=env,stdout=log,stderr=log)
        host=psutil.Process(run.pid);children=set();started=time.monotonic()
        while run.poll() is None:
            if time.monotonic()-started>180:
                for c in host.children(recursive=True):c.kill()
                host.kill();raise TimeoutError('private storage UI test')
            try:children.update((c.pid,c.create_time()) for c in host.children(recursive=True))
            except psutil.Error:pass
            time.sleep(.05)
    left=[]
    for pid,created in children:
        try:
            c=psutil.Process(pid)
            if c.create_time()==created and c.is_running():left.append(pid)
        except psutil.Error:pass
    path=out/'storage_ui_report.json';report=json.loads(path.read_text(encoding='utf-8'))
    if run.returncode==0 and report['passed']:
        cli=[]
        for mode,source in [('upgrade',out/'legacy.sqlite3'),('legacy',out/'cli-upgrade.sqlite3')]:
            receipt=out/('cli-'+mode+'.json');target=out/('cli-'+mode+'.sqlite3');before=hashlib.sha256(source.read_bytes()).hexdigest()
            result=subprocess.run(command+['--storage-copy',mode,str(source),str(target),'--report',str(receipt)],cwd=out,env=env,timeout=120)
            data=json.loads(receipt.read_text(encoding='utf-8'));assert result.returncode==0 and data['passed']
            assert hashlib.sha256(source.read_bytes()).hexdigest()==before;cli.append(data)
        assert cli[0]['checks']==cli[1]['checks'];report['cli_roundtrip']=cli;report['checks'].append('native CLI upgrade and legacy roundtrip exact, no UI recursion, source unchanged')
    report.update(exit_code=run.returncode,remaining_children=left,development=not bool(a.exe),exe_sha256=hashlib.sha256(a.exe.read_bytes()).hexdigest() if a.exe else None)
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(report,ensure_ascii=False))
    assert run.returncode==0 and report['passed'] and not left

if __name__=='__main__':main()
