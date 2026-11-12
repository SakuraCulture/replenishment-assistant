"""Drive real native prepare/apply and reopen using new isolated fixtures."""
import argparse,copy,hashlib,json,os,sqlite3,subprocess,sys,time
from contextlib import closing
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]

def signature(root):
    path=root/'workbench.sqlite3'
    if not path.is_file():return None
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db:
        return {table:hashlib.sha256(repr(db.execute('SELECT * FROM '+table+' ORDER BY 1,2').fetchall()).encode()).hexdigest() for table in ('normal_orders','normal_revisions','normal_work_revisions','snapshot_chunks','source_files')}

def seed(folder,count):
    import test_archive_orders as f,normal_orders as no,work_repository as wr
    folder.mkdir(parents=True,exist_ok=True);work,repo,payload=f.seed(folder)
    with closing(work.connect()) as db,db:
        for i in range(1,count):
            p=copy.deepcopy(payload);code=f'T{i:03}';p['result'].update(store=code,store_name='合成门店'+code)
            db.execute('INSERT INTO normal_orders VALUES(?,?,?,?,?,?)',(wr.today(),code,'normal_model','oid-'+code,'BH-'+code+'-ZC',1))
            db.execute('INSERT INTO normal_revisions VALUES(?,?,?,?,?)',('oid-'+code,1,no.fingerprint_owned(p['result'],p.get('new_focus')),wr.now(),no.encode(p,db)))
    return work

def main():
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);p.add_argument('--exe',type=Path);p.add_argument('--case',default='all');a=p.parse_args()
    out=a.out.resolve();out.mkdir(parents=True,exist_ok=False)
    # Never accept an existing or real root as input. Every directory is seeded here.
    results=[]
    def command(folder,case):return ([str(a.exe.resolve())] if a.exe else [sys.executable,str(ROOT/'src/app.py')])+['--migration-shutdown-smoke',str(folder),case]
    def env(folder):return {**os.environ,'REPLENISHMENT_DATA_DIR':str(folder/'settings'),'LOCALAPPDATA':str(folder/'localappdata'),'QT_QPA_PLATFORM':'windows','PYTHONUTF8':'1'}
    def run(folder,case):
        with (folder/(case+'-process.log')).open('wb') as log:
            process=subprocess.Popen(command(folder,case),cwd=ROOT,env=env(folder),stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW)
            try:code=process.wait(timeout=110)
            except subprocess.TimeoutExpired:raise TimeoutError(f'Owned isolated process {process.pid} retained; not force-killed')
            finally:
                if process.poll() is not None and hasattr(process,'_handle'):process._handle.Close()
        return code
    import order_migration as migration,migration_status as status
    cases=['confirm','cancel','async','background_fail','apply_failure','shutdown_exception','early_loop_exit','interrupt','hold','caught_background'] if a.case=='all' else [a.case]
    for case in cases:
        folder=out/case;folder.mkdir();source=seed(folder/'source',18);seed(folder,2)
        # One real linked task identity, paused by the real import implementation.
        file=source.path.parent/'aoxiang/workflows.sqlite3';file.parent.mkdir(exist_ok=True)
        with closing(sqlite3.connect(file)) as db,db:
            db.execute('CREATE TABLE ax_workflows(id TEXT PRIMARY KEY,state TEXT,payload TEXT)')
            db.execute('INSERT INTO ax_workflows VALUES(?,?,?)',('linked','RUNNING',json.dumps({'state':'RUNNING','items':[{'oid':'oid','platform_order':'PO-SYNTHETIC-001','state':'BOUND'}]})))
        migration.export(source.path.parent,folder/'orders.zip');(folder/'synthetic.json').write_text('{"synthetic":true,"live_platform":false}','utf-8')
        old=signature(folder/'settings');expected=signature(source.path.parent)
        if case=='hold':
            with (folder/'hold-process.log').open('wb') as log:
                first=subprocess.Popen(command(folder,'hold'),cwd=ROOT,env=env(folder),stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW)
                deadline=time.monotonic()+45
                while not (folder/'hold-ready').exists() and first.poll() is None and time.monotonic()<deadline:time.sleep(.05)
                assert (folder/'hold-ready').is_file(),'First native instance not ready'
                owner=(folder/'hold-ready').read_text('utf-8');second=run(folder,'hold-second')
                assert second==0 and first.poll() is None and (folder/'hold-ready').read_text('utf-8')==owner,'Second writer opened or original lost'
                assert not (folder/'hold-second-events.jsonl').exists(),'Second reached application data window'
                (folder/'hold-stop').touch();code=first.wait(timeout=30)
                if hasattr(first,'_handle'):first._handle.Close()
                assert code==0
            results.append({'case':case,'passed':True,'second_launch_activated_original':True});continue
        code=run(folder,case);value=status.latest(folder/'settings');assert value is not None,case
        if case in ('confirm','async','caught_background'):
            assert code==0 and value['state']=='COMPLETED',(case,code,value)
            assert signature(folder/'settings')==expected and signature(Path(value['backup']))==old
            with closing(sqlite3.connect(folder/'settings/aoxiang/workflows.sqlite3')) as db:
                state,raw=db.execute('SELECT state,payload FROM ax_workflows').fetchone();assert state=='PAUSED' and json.loads(raw)['items'][0]['platform_order']=='PO-SYNTHETIC-001'
            assert run(folder,'reopen-completed')==0;assert signature(folder/'settings')==expected
        elif case=='cancel':assert code==0 and value['state']=='CANCELLED' and signature(folder/'settings')==old and (folder/'orders.zip').is_file()
        elif case=='interrupt':
            assert code==73 and not (folder/'settings').exists() and Path(value['backup']).is_dir()
            assert run(folder,'reopen-interrupted')==0;assert signature(folder/'settings')==old
            value=status.latest(folder/'settings');assert value['state']=='FAILED' and Path(value['stage']).is_dir()
        else:
            assert code==(0 if case=='background_fail' else 1) and value['state']=='FAILED',(case,code,value)
            assert signature(folder/'settings')==old and Path(value['stage']).is_dir() and (folder/'orders.zip').is_file()
            if case=='background_fail':assert 'BACKGROUND-ORIGINAL-EXIT-ERROR' in value['error']['message']
            if case=='apply_failure':assert 'ORIGINAL-APPLY-COPY-ERROR' in value['error']['traceback']
            if case=='shutdown_exception':assert 'ORIGINAL-DESTRUCTOR-ERROR' in (folder/'.settings.diagnostics/application.jsonl').read_text('utf-8')
            assert run(folder,'reopen-failed')==0;assert signature(folder/'settings')==old
        messages=[]
        for file in folder.glob('*-events.jsonl'):
            events=[json.loads(line) for line in file.read_text('utf-8').splitlines()]
            assert not any(e['event']=='harness_exception' for e in events),events
            messages.extend(e for e in events if e['event']=='real_message')
        assert all(e['phase']=='RUNNING' for e in messages),'A shutdown path opened a native dialog'
        results.append({'case':case,'passed':True,'exit_code':code,'state':value['state'],'real_native_dialogs':len(messages),'source_orders':18,'original_orders':2})
        (out/'progress.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),'utf-8')
    result={'passed':True,'cases':results,'frozen_exe':bool(a.exe),'live_platform':False}
    if a.exe:result['exe_sha256']=hashlib.sha256(a.exe.read_bytes()).hexdigest()
    (out/'report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),'utf-8');print(json.dumps(result,ensure_ascii=False))

if __name__=='__main__':
    import multiprocessing
    multiprocessing.freeze_support();main()
