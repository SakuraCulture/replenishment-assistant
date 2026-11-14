"""Run native location dialogs, post-exit copy, recovery and reopen on synthetic data."""
import argparse,hashlib,json,os,sqlite3,subprocess,sys,time
from contextlib import closing
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]

def signature(folder):
    with closing(sqlite3.connect((folder/'workbench.sqlite3').as_uri()+'?mode=ro',uri=True)) as db:
        return {t:hashlib.sha256(repr(db.execute('SELECT * FROM '+t+' ORDER BY 1,2').fetchall()).encode()).hexdigest() for t in ('normal_orders','normal_revisions','normal_work_revisions','snapshot_chunks','source_files')}

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--out',type=Path,required=True);parser.add_argument('--exe',type=Path);parser.add_argument('--case',default='all');args=parser.parse_args()
    out=args.out.resolve();out.mkdir(parents=True,exist_ok=False)
    # Private LOCALAPPDATA is on C and target is on the output disk (D in acceptance).
    results=[]
    cases=['confirm','cancel','unsaved_cancel','async','background_fail','shutdown_exception','apply_failure','cleanup_failure','interrupt_before','interrupt_after','busy','viewer_failure','large_file','cwd_in_source','qt_handle','login_pause','hot_journal','corrupt_database','external_journal'] if args.case=='all' else [args.case]
    import data_relocation as move
    for case in cases:
        folder=out/case;folder.mkdir();local=Path(os.environ['TEMP'])/('replenishment-location-'+os.urandom(8).hex())
        local.mkdir();source=local/'YoutuanTools/补货助手';source.mkdir(parents=True)
        import test_archive_orders as fixture
        fixture.seed(source)
        with closing(sqlite3.connect(source/'settings/closed-wal.sqlite3')) as db,db:
            db.execute('PRAGMA journal_mode=WAL');db.execute('CREATE TABLE evidence(value)');db.execute("INSERT INTO evidence VALUES('clean-wal-preserved')")
        assert not Path(str(source/'settings/closed-wal.sqlite3')+'-wal').exists()
        (source/'settings/analysis-originals').mkdir();(source/'settings/analysis-originals/original.bin').write_bytes(b'analysis-frozen-original')
        profile=source/'settings/jst_catalog_sync/browsers/sd/User Data/Default';profile.mkdir(parents=True)
        (profile.parent/'Local State').write_text('synthetic-login','utf-8')
        (source/'AI接入').mkdir();(source/'AI接入/registration.json').write_text('{"synthetic":true}','utf-8')
        frozen_database={};db_name='settings/jst_catalog_sync/browsers/hb/User Data/Default/ExtensionActivityEdge'
        if case in ('hot_journal','corrupt_database','external_journal'):
            candidate=source/db_name;candidate.parent.mkdir(parents=True)
            if case=='corrupt_database':candidate.write_bytes(b'SQLite format 3\0'+b'\xff'*4096)
            else:
                from test_relocation_integrity import seed_hot_journal,JOURNAL_MAGIC
                seed_hot_journal(candidate)
                if case=='external_journal':
                    sentinel=folder/'external-super-journal.keep';sentinel.write_bytes(b'SYNTHETIC-EXTERNAL-KEEP')
                    identity=str(sentinel).encode('utf-8')
                    with Path(str(candidate)+'-journal').open('ab') as file:
                        file.write(identity+len(identity).to_bytes(4,'big')+(sum(identity)&0xffffffff).to_bytes(4,'big')+JOURNAL_MAGIC)
            for suffix in ('','-journal'):
                path=Path(str(candidate)+suffix)
                if path.is_file():frozen_database[db_name+suffix]=hashlib.sha256(path.read_bytes()).hexdigest()
        target=folder/'D盘新资料';(folder/'synthetic.json').write_text(json.dumps({'synthetic':True,'live_platform':False,'target':str(target),'source':str(source),'localappdata':str(local)},ensure_ascii=False),'utf-8')
        expected=signature(source/'settings')
        environment={**os.environ,'LOCALAPPDATA':str(local),'QT_QPA_PLATFORM':'windows','PYTHONUTF8':'1'};environment.pop('REPLENISHMENT_DATA_DIR',None)
        # Marker and private LOCALAPPDATA agree exactly across physical volumes.
        def command(mode):return ([str(args.exe.resolve())] if args.exe else [sys.executable,str(ROOT/'src/app.py')])+['--data-location-smoke',str(folder),mode]
        def start(mode):
            file=(folder/(mode+'-process.log')).open('wb')
            process=subprocess.Popen(command(mode),cwd=ROOT,env=environment,stdout=file,stderr=file,creationflags=subprocess.CREATE_NO_WINDOW)
            return process,file
        def wait(process,file):
            try:return process.wait(timeout=125)
            except subprocess.TimeoutExpired:raise TimeoutError(f'Owned synthetic process {process.pid} retained, not force-killed')
            finally:
                file.close()
                if process.poll() is not None and hasattr(process,'_handle'):process._handle.Close()
        def run(mode):return wait(*start(mode))
        def journal():return json.loads((local/'YoutuanTools/data-location/move.json').read_text('utf-8'))
        if case=='busy':
            process,file=start(case);deadline=time.monotonic()+65
            while not (folder/'copy-ready').exists() and process.poll() is None and time.monotonic()<deadline:time.sleep(.05)
            assert (folder/'copy-ready').is_file(),'Copy stage not reached'
            second=run('busy-second');assert second==0 and process.poll() is None,('Read-only progress launch failed',second)
            seen=[json.loads(line) for line in (folder/'busy-second-progress-events.jsonl').read_text('utf-8').splitlines()]
            assert any(e['event']=='opened' and e['visible'] for e in seen),'No real progress window appeared'
            assert not (folder/'busy-second-events.jsonl').exists(),'A second business window was initialized'
            (folder/'copy-release').touch();code=wait(process,file)
        else:code=run(case)
        value=journal()
        if case in ('confirm','async','cleanup_failure','busy','large_file','cwd_in_source','qt_handle','login_pause','hot_journal'):
            assert code==0 and value['state']=='COMPLETED',(case,code,value)
            deadline=time.monotonic()+45
            while not (folder/'reopen-exited').exists() and time.monotonic()<deadline:time.sleep(.05)
            assert (folder/'reopen-ready').is_file() and (folder/'reopen-exited').is_file(),'Automatic reopen did not finish'
            assert Path((folder/'reopen-ready').read_text('utf-8')).resolve()==(target/'settings').resolve()
            assert signature(target/'settings')==expected and source.is_junction()
            if case=='cleanup_failure':
                assert value['phase']=='CLEANUP_PENDING' and Path(value['backup']).is_dir()
            else:
                with __import__('unittest').mock.patch.dict(os.environ,LOCALAPPDATA=str(local)):
                    move.verify_archive(value['archive'],json.loads(Path(value['plan_file']).read_text('utf-8')))
            assert (target/'settings/analysis-originals/original.bin').read_bytes()==b'analysis-frozen-original'
            assert (target/'settings/jst_catalog_sync/browsers/sd/User Data/Default').is_dir()
            with closing(sqlite3.connect((target/'settings/closed-wal.sqlite3').as_uri()+'?mode=ro',uri=True)) as db:
                assert db.execute('SELECT value FROM evidence').fetchone()[0]=='clean-wal-preserved'
            if case=='confirm':
                # A missing chosen disk must show a real startup error, without
                # opening an empty default store or silently reverting location.
                offline=target.with_name('offline-test');target.rename(offline)
                assert run('reopen-offline')==1
                assert not target.exists() and (offline/'settings/workbench.sqlite3').is_file()
                offline.rename(target)
        elif case in ('cancel','unsaved_cancel','viewer_failure'):assert code==0 and value['state']=='CANCELLED' and signature(source/'settings')==expected and not target.exists()
        elif case.startswith('interrupt'):
            assert code==73
            assert run('reopen-recovered')==0
            value=journal()
            assert signature((target if case=='interrupt_after' else source)/'settings')==expected
            assert value['state']==('COMPLETED' if case=='interrupt_after' else 'FAILED')
        else:
            assert code==(0 if case=='background_fail' else 1) and value['state']=='FAILED',(case,code,value)
            assert signature(source/'settings')==expected and not source.is_junction()
            assert run('reopen-failed')==0
        if frozen_database:
            preserved=target if case=='hot_journal' else source
            for name,digest in frozen_database.items():assert hashlib.sha256((preserved/name).read_bytes()).hexdigest()==digest,('Changed DB or journal',name)
            assert not list(folder.glob('.sqlite-check-*')),'Verification scratch left behind'
            if case=='hot_journal':
                import zipfile
                with zipfile.ZipFile(value['archive']) as archive:
                    for name,digest in frozen_database.items():assert hashlib.sha256(archive.read('data/'+name)).hexdigest()==digest
            else:
                assert db_name in value['error']['message'],'Missing failing relative database name'
                if case=='external_journal':assert (folder/'external-super-journal.keep').read_bytes()==b'SYNTHETIC-EXTERNAL-KEEP'
        messages=[]
        if case not in ('cancel','unsaved_cancel','viewer_failure'):
            observer=folder/(case+'-progress-events.jsonl');deadline=time.monotonic()+20
            while time.monotonic()<deadline:
                seen=[json.loads(line) for line in observer.read_text('utf-8').splitlines()] if observer.is_file() else []
                if any(e['event']=='closed' for e in seen):break
                time.sleep(.1)
            assert any(e['event']=='opened' and e['visible'] for e in seen),'No independent progress window appeared'
            assert all(e.get('controls_fit',True) for e in seen if e['event']=='opened'),'Progress controls do not fit the native window'
            assert any(e['event']=='closed' for e in seen),'Synthetic progress window did not exit'
            assert not any(e['event']=='exception' for e in seen),'A progress callback raised an exception'
            assert not (local/'YoutuanTools/data-location/diagnostics/progress-errors.jsonl').exists(),'Progress exception was logged'
            assert any(e['event']=='displayed' and e['state'] in ('COMPLETED','FAILED') for e in seen),seen
            if case=='large_file':assert any(e['event']=='advanced' and e['phase']=='COPYING' and 0<e['percent']<100 and 'MiB' in e['counts'] for e in seen),seen
        for path in folder.glob('*-events.jsonl'):
            events=[json.loads(line) for line in path.read_text('utf-8').splitlines()]
            assert not any(e['event'] in ('harness_exception','watchdog_safe_close','unexpected_auto_connection') for e in events),events
            messages.extend(e for e in events if e['event']=='real_message')
        assert all(e['phase']=='RUNNING' for e in messages),'A cleanup path created a widget'
        if case in ('confirm','async','cleanup_failure','busy','large_file','cwd_in_source','qt_handle','login_pause','hot_journal'):
            events=[json.loads(line) for line in (folder/(case+'-events.jsonl')).read_text('utf-8').splitlines()]
            assert any(e['event']=='copy_after_qt_destroyed' and e['instance_guard_retained'] for e in events),'No verified Qt-free, guarded copy'
            if case=='qt_handle':assert any(e['event']=='qt_directory_released' for e in events),'Application-owned directory handle was not released'
        result={'case':case,'passed':True,'code':code,'state':value['state'],'phase':value['phase'],'source':str(source),'target':str(target),'cross_volume':source.anchor!=target.anchor,'native_dialogs':len(messages)}
        results.append(result);(out/'progress.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),'utf-8')
    report={'passed':True,'cases':results,'frozen_exe':bool(args.exe),'live_platform':False}
    if args.exe:report['exe_sha256']=hashlib.sha256(args.exe.read_bytes()).hexdigest()
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8');print(json.dumps(report,ensure_ascii=False))

if __name__=='__main__':
    import multiprocessing,unittest.mock
    multiprocessing.freeze_support();main()
