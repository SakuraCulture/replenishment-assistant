"""Supervise copy-only maintenance through source or actual Windows EXE."""
import argparse,hashlib,json,os,sqlite3,subprocess,sys,time,zlib
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
                raise TimeoutError('private storage UI test still active; no process was killed')
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
        source=out/'backup.sqlite3';target=out/'cli-backup.sqlite3';receipt=out/'cli-backup.json';before=hashlib.sha256(source.read_bytes()).hexdigest()
        result=subprocess.Popen(command+['--storage-copy','backup',str(source),str(target),'--report',str(receipt)],cwd=out,env=env);result.wait(timeout=120)
        data=json.loads(receipt.read_text(encoding='utf-8'));assert result.returncode==0 and data['passed']
        assert hashlib.sha256(source.read_bytes()).hexdigest()==before
        with sqlite3.connect(source) as a,sqlite3.connect(target) as b:assert list(a.iterdump())==list(b.iterdump())
        report['cli_backup']=data;report['checks'].append('native CLI backup preserves every SQL value and source hash')
        old=out/'unsupported.sqlite3'
        with sqlite3.connect(old) as db:db.execute('CREATE TABLE jobs(payload BLOB)');db.execute('INSERT INTO jobs VALUES(?)',(zlib.compress(b'{}'),))
        before=hashlib.sha256(old.read_bytes()).hexdigest();rejected=out/'rejected.sqlite3';receipt=out/'rejected.json'
        result=subprocess.Popen(command+['--storage-copy','backup',str(old),str(rejected),'--report',str(receipt)],cwd=out,env=env);result.wait(timeout=120)
        data=json.loads(receipt.read_text('utf-8'));assert result.returncode==1 and not data['passed'] and '已停用' in data['error']
        assert not rejected.exists() and hashlib.sha256(old.read_bytes()).hexdigest()==before
        report['checks'].append('native CLI rejects retired format, leaves source untouched and publishes no database')
    report.update(exit_code=run.returncode,remaining_children=left,development=not bool(a.exe),exe_sha256=hashlib.sha256(a.exe.read_bytes()).hexdigest() if a.exe else None)
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(report,ensure_ascii=False))
    assert run.returncode==0 and report['passed'] and not left

if __name__=='__main__':main()
