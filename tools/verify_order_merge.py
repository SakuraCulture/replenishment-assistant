"""New-directory-only actual Qt/EXE merge, failure, recovery and dedup acceptance."""
import argparse,copy,hashlib,json,os,shutil,sqlite3,subprocess,sys,time
from contextlib import closing
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]


def read(p):return json.loads(Path(p).read_text('utf-8'))
def write(p,v):Path(p).write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n','utf-8')
def hash_file(p):
    with Path(p).open('rb') as file:return hashlib.file_digest(file,'sha256').hexdigest()
def table(path,name,where='',args=()):
    with closing(sqlite3.connect(Path(path).as_uri()+'?mode=ro',uri=True)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(name,)).fetchone():return []
        return db.execute('SELECT * FROM "'+name+'"'+(' WHERE '+where if where else '')+' ORDER BY rowid',args).fetchall()
def signed(root):
    import normal_orders,work_repository as wr
    repo=normal_orders.Repository(wr.Repository(root))
    return {h['oid']:wr.dumps(repo.get(h['day'],h['store'])) for h in repo.headers()}
def local_facts(root,oids):
    path=Path(root)/'workbench.sqlite3';values={}
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db:
        names={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for name in sorted(names):
            if name.startswith('sqlite_'):continue
            cols=[r[1] for r in db.execute('PRAGMA table_info("'+name+'")')]
            if 'oid' in cols:
                sql='SELECT * FROM "'+name+'" WHERE oid IN ('+','.join('?' for _ in oids)+') ORDER BY rowid'
                values[name]=db.execute(sql,list(oids)).fetchall()
            elif name in ('jobs','presets','stores','jst_warehouses'):
                values[name]=db.execute('SELECT * FROM "'+name+'" ORDER BY rowid').fetchall()
    # An empty newly-created schema does not alter existing business values.
    return {k:v for k,v in values.items() if v}
def lifecycle(root,head):
    import order_lifecycle,work_repository as wr
    view=order_lifecycle.snapshot(Path(root)/'workbench.sqlite3',head)
    return {k:wr.dumps(view[k]) for k in ('rows','present','status','next')}


def component_probe(component,root,purchase_id,machine_id,*,register=False):
    """Open real local protocol classes; prove paused work cannot dispatch."""
    component=Path(component).resolve();node=component/'runtime/node.exe'
    script="""import {StateStore} from './src/state.mjs';
import {PurchaseCreates} from './src/purchase-create-ledger.mjs';
import {PurchaseReceipts} from './src/purchase-receipt-ledger.mjs';
import {MachineCommands} from './src/machine-commands.mjs';
import {MachineRuntime} from './src/machine-runtime.mjs';
const [root,pid,mid,register]=process.argv.slice(1),s=await StateStore.open(root);
try{
 const creates=await PurchaseCreates.open(s),receipts=await PurchaseReceipts.open(s),commands=new MachineCommands(s),p=creates.get(pid);
 if(register==='yes')receipts.register(p,'2026-11-13');
 const visits=[],runtime=new MachineRuntime({commands,executeItem:async x=>{visits.push(x.item.run_id);throw Error('Unexpected imported automatic dispatch');}});
 if(register!=='yes'){runtime.kick();await runtime.idle();}
 const result={node:process.version,purchase:p,receipt:receipts.get(pid),machine:commands.get(mid),machineEvents:commands.events(mid),visits};
 if(register!=='yes')await runtime.shutdown();console.log(JSON.stringify(result));
}finally{s.close();}
"""
    env={**os.environ,'REPLENISHMENT_BUSINESS_DB':str(Path(root).parents[1]/'workbench.sqlite3')}
    process=subprocess.run([str(node),'--input-type=module','-e',script,str(root),purchase_id,machine_id,'yes' if register else 'no'],cwd=component,env=env,capture_output=True,encoding='utf-8',creationflags=subprocess.CREATE_NO_WINDOW)
    if process.returncode:raise AssertionError('Packaged/local component protocol open failed: '+process.stderr)
    value=json.loads(process.stdout);value['node_sha256']=hash_file(node);return value


def seed(folder,case=None):
    from merge_fixture import Order,second_order,purchase_ledger,machine_command
    import normal_orders,work_repository as wr,warehouse_catalog
    from test_jst import catalog
    local=Order(folder,code='COMMON');second_order(local,'LOCAL');second_order(local,'CLASH',oid='local-clash-identity')
    local.work.save_preset('本机参数保留',__import__('parameter_schema').standard())
    warehouse_catalog.Repository(local.root).save(catalog(stock=999))
    for name,text in [('connection.json','{"synthetic":"local-connection-keep"}'),('analysis-independent/keep.bin','local-analysis-keep')]:
        p=local.root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(text,'utf-8')
    source=Order(folder/'source',code='NEW');source.complete(stock=7)
    import normal_exports,export_receipts
    exported=source.folder/'synthetic-signed-NEW.xlsx'
    normal_exports.export(source.signed(),exported,warehouse='河北仓')
    receipt=export_receipts.begin(source.path,'merchant',[{'path':str(exported.resolve()),'state':'已生成','stage':'完成','head':export_receipts.identity(source.signed(),'河北仓')}],signature='合成原修订签发单导出')
    purchase=purchase_ledger(source,platform_id='PO-SYNTHETIC-NATIVE-0528')
    machine=machine_command(source)['record']
    protocols=component_probe(ROOT/'components/aoxiang',source.root/'aoxiang/component-state',purchase['id'],machine['id'],register=True)
    # Add one exact duplicate signed chain, copied before any native launch.
    with closing(sqlite3.connect(source.path)) as db,db:
        db.execute('ATTACH DATABASE ? AS old',(str(local.path),))
        for name in ('snapshot_chunks','source_files'):
            db.execute('INSERT OR IGNORE INTO '+name+' SELECT * FROM old.'+name)
        db.execute("INSERT INTO normal_orders SELECT * FROM old.normal_orders WHERE store='COMMON'")
        db.execute('INSERT INTO normal_revisions SELECT * FROM old.normal_revisions WHERE oid=?',(local.oid,))
        db.execute('INSERT INTO normal_work_revisions SELECT * FROM old.normal_work_revisions WHERE revision=?',('work-'+local.oid,))
    second_order(source,'CLASH',oid='source-clash-identity')
    if case=='source-deleted':
        # The source itself supplies this contradictory order/tombstone state;
        # export normally so every manifest/hash is valid. Never tamper with a
        # prepared package or use an unverified staging shortcut.
        import deletion_barrier
        with closing(sqlite3.connect(source.path)) as db,db:
            deletion_barrier.schema(db)
            db.execute('INSERT INTO deleted_order_tokens VALUES(?,?)',(source.oid,'d'*32))
            db.execute('INSERT INTO deletion_generations VALUES(?,?,1)',(source.day,source.code))
    source.export(folder/'orders.zip')
    source_before=signed(source.root);old=signed(local.root)
    files={name:hash_file(local.root/name) for name in ('connection.json','analysis-independent/keep.bin','warehouse_catalogs/河北仓.json')}
    initial={'added':0,'duplicate':1,'conflict':2,'deleted':0} if case=='source-deleted' else {'added':1,'duplicate':1,'conflict':1,'deleted':0}
    marker={'synthetic':True,'live_platform':False,'initial_counts':initial,'repeat_counts':{'added':0,'duplicate':2,'conflict':1,'deleted':0},'reopen_parent_count':3}
    write(folder/'synthetic.json',marker)
    return {'local':local,'source':source,'old':old,'source_before':source_before,'old_facts':local_facts(local.root,old),'files':files,'new_signed':source_before[source.oid],'new_lifecycle':lifecycle(source.root,source.h),'package_sha':hash_file(folder/'orders.zip'),'export_receipt':receipt,'exported_sha':hash_file(exported),'purchase':purchase,'machine':machine,'source_protocols':protocols}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--out',type=Path,required=True);parser.add_argument('--exe',type=Path);parser.add_argument('--case',default='all');parser.add_argument('--timeout',type=float,default=125)
    args=parser.parse_args();out=args.out.resolve();out.mkdir(parents=True,exist_ok=False)
    cases=['confirm','cancel','async','background_fail','apply_failure','interrupt-before','interrupt-after','source-deleted'] if args.case=='all' else [args.case]
    import order_migration as migration,migration_status as status
    results=[]
    def run(folder,mode,parents):
        marker=read(folder/'synthetic.json');marker['reopen_parent_count']=parents;write(folder/'synthetic.json',marker)
        environment={**os.environ,'REPLENISHMENT_DATA_DIR':str(folder/'settings'),'LOCALAPPDATA':str(folder/'localappdata'),'APPDATA':str(folder/'roaming'),'QT_QPA_PLATFORM':'windows','PYTHONUTF8':'1'}
        for key in ('PYTHONPATH','PYTHONHOME','QT_PLUGIN_PATH','REPLENISHMENT_PORTABLE_PATHS'):environment.pop(key,None)
        command=([str(args.exe.resolve())] if args.exe else [sys.executable,str(ROOT/'src/app.py')])+['--order-merge-smoke',str(folder),mode]
        with (folder/(mode+'-process.log')).open('wb') as stream:
            process=subprocess.Popen(command,cwd=ROOT,env=environment,stdout=stream,stderr=stream,creationflags=subprocess.CREATE_NO_WINDOW)
            try:return process.wait(timeout=args.timeout)
            except subprocess.TimeoutExpired:
                write(folder/(mode+'-timeout.json'),{'pid':process.pid,'mode':mode,'not_force_terminated':True});raise
            finally:
                if process.poll() is not None and hasattr(process,'_handle'):process._handle.Close()
    def assert_old(folder,f):
        values=signed(folder/'settings')
        for oid,value in f['old'].items():assert values[oid]==value,('Changed existing signed order',oid)
        assert local_facts(folder/'settings',f['old'])==f['old_facts'],'Changed existing lifecycle/settings facts'
        for name,h in f['files'].items():assert hash_file(folder/'settings'/name)==h,('Changed local file',name)
        assert hash_file(folder/'orders.zip')==f['package_sha'],'Changed package bytes'
    def assert_success(folder,f):
        assert_old(folder,f);values=signed(folder/'settings')
        assert len(values)==4 and values[f['source'].oid]==f['new_signed'],'New signed payload missing or modified'
        assert lifecycle(folder/'settings',f['source'].h)==f['new_lifecycle'],'New complete lifecycle differs'
        value=status.latest(folder/'settings');assert value and value['state']=='COMPLETED',value
        assert signed(Path(value['backup']))==f['old'],'Full old signed backup differs'
        assert not migration.journal(folder/'settings').exists(),'Handoff journal not finalized'
        receipt=read(folder/'settings/订单迁入完成.json');assert receipt['mode']=='merge'
        assert receipt['report']['counts']==read(folder/'synthetic.json')['initial_counts'],receipt
        import export_receipts,portable_assets
        retained=export_receipts.recent(folder/'settings/workbench.sqlite3')
        exported=next((r for r in retained if r['id']==f['export_receipt']['id']),None)
        assert exported is not None,'Imported actual export receipt is not visible to the real reader'
        assert exported['database']==str((folder/'settings/workbench.sqlite3').resolve()),'Receipt still bound to staging database'
        path=portable_assets.resolve(exported['files'][0]['path'],mapping=folder/'settings/迁入原文件映射.json')
        assert path.is_file() and hash_file(path)==f['exported_sha'],'Exported original bytes or portable mapping missing'
        mapping=read(folder/'settings/迁入原文件映射.json')['paths']
        stored=folder/'settings'/mapping[exported['files'][0]['path']]
        assert stored.is_file() and hash_file(stored)==f['exported_sha'],'No independently retained exported original'
        component=args.exe.parent/'_internal/components/aoxiang' if args.exe else ROOT/'components/aoxiang'
        protocols=component_probe(component,folder/'settings/aoxiang/component-state',f['purchase']['id'],f['machine']['id'])
        write(folder/'component-open-report.json',protocols)
        assert protocols['purchase']['binding']==f['purchase']['binding'] and protocols['purchase']['intent']==f['purchase']['intent'],'Imported purchase identity changed'
        assert protocols['purchase']['receipt']==f['purchase']['receipt'] and protocols['purchase']['uploadAttempted'],'Accepted request evidence missing'
        assert protocols['receipt']==f['source_protocols']['receipt'],'Receipt terminal/scope fact changed'
        assert protocols['machine']['state']=='PAUSED' and protocols['machine']['request']==f['machine']['request'],'Machine command not safely paused'
        assert protocols['visits']==[],'Imported machine command dispatched automatically'
        assert protocols['machineEvents'][-1]['kind']=='MIGRATION_PAUSED','Explicit pause audit event missing'
    for case in cases:
        folder=out/case;folder.mkdir();f=seed(folder,case)
        code=run(folder,case,3);value=status.latest(folder/'settings')
        initial_events=[json.loads(line) for line in (folder/(case+'-events.jsonl')).read_text('utf-8').splitlines()]
        harness=[e for e in initial_events if e['event'] in ('harness_exception','watchdog_safe_close')]
        assert not harness,('Native acceptance harness failed',harness)
        if case in ('confirm','async'):
            assert code==0,(case,code,value);assert_success(folder,f)
            assert run(folder,'reopen-completed',4)==0;assert_success(folder,f)
            if case=='confirm':
                pre=signed(folder/'settings');facts=local_facts(folder/'settings',pre)
                assert run(folder,'repeat',4)==0 and signed(folder/'settings')==pre and local_facts(folder/'settings',pre)==facts
                assert not getattr(value,'migration_running',False)
                f['source'].export(folder/'reexport.zip')
                assert run(folder,'reexport',4)==0 and signed(folder/'settings')==pre and local_facts(folder/'settings',pre)==facts
        elif case in ('cancel','source-deleted'):
            assert code==0 and value and value['state']=='CANCELLED',(case,code,value)
            assert signed(folder/'settings')==f['old'];assert_old(folder,f)
            if case=='source-deleted':
                preview=next(e for e in initial_events if e['event']=='preview_verified')
                assert preview['counts']=={'added':0,'duplicate':1,'conflict':2,'deleted':0} and not preview['confirm_enabled']
                new=next(r for r in preview['rows'] if r[1]=='NEW')
                assert new[4]=='有差异，保留本机' and ('失效' in new[5] or '删除' in new[5]),new
                assert any(e['event']=='no_handoff' and e['enabled'] and not e['pending'] for e in initial_events)
                assert not (folder/'settings/订单迁入完成.json').exists(),'Blocked source order produced a completion receipt'
                assert not (folder/'settings/order-merge-reports').exists(),'Blocked source order produced a published merge report'
        elif case=='interrupt-before':
            assert code==73 and value and Path(value['backup']).is_dir() and not (folder/'settings').exists(),(case,code,value)
            assert run(folder,'reopen-rolled-back',3)==0
            assert signed(folder/'settings')==f['old'];assert_old(folder,f)
            value=status.latest(folder/'settings');assert value['state']=='FAILED' and Path(value['stage']).is_dir(),value
        elif case=='interrupt-after':
            assert code==74 and value and (folder/'settings/订单迁入完成.json').is_file(),(case,code,value)
            assert run(folder,'reopen-finalize',4)==0;assert_success(folder,f)
        else:
            assert code==(0 if case=='background_fail' else 1) and value and value['state']=='FAILED',(case,code,value)
            assert signed(folder/'settings')==f['old'];assert_old(folder,f)
            assert Path(value['stage']).is_dir() and (folder/'orders.zip').is_file()
            expected='SYNTHETIC-MERGE-BACKGROUND-EXIT-ERROR' if case=='background_fail' else 'SYNTHETIC-MERGE-APPLY-COPY-ERROR'
            assert expected in value.get('error',{}).get('traceback',''),value
            assert run(folder,'reopen-failed',3)==0;assert_old(folder,f)
        all_events=[]
        for path in folder.glob('*-events.jsonl'):
            events=[json.loads(line) for line in path.read_text('utf-8').splitlines()]
            assert not any(e['event'] in ('harness_exception','watchdog_safe_close') for e in events),events
            all_events.extend(events)
        assert any(e['event']=='preview_verified' for e in all_events),'Real preview not exercised'
        assert all(e['phase']=='RUNNING' for e in all_events if e['event']=='real_message'),'A cleanup path constructed a Qt dialog'
        if case=='confirm':
            for mode in ('repeat','reexport'):
                assert any(e['event']=='no_handoff' and e['mode']==mode and e['enabled'] and not e['pending'] for e in all_events),'No-additions path incorrectly requested shutdown'
        if case=='async':assert len([e for e in all_events if e['event']=='synthetic_shutdown_call'])>=2
        assert signed(f['source'].root)==f['source_before'],'Source order content changed'
        latest_state=status.latest(folder/'settings')['state']
        primary_state='COMPLETED' if case in ('confirm','async','interrupt-after') else latest_state
        results.append({'case':case,'passed':True,'exit_code':code,'state':primary_state,'initial_import_state':value['state'],'latest_followup_state':latest_state,'repeat_no_additions_cancelled_preview':case=='confirm','native_preview':True,'original_parents':3,'source_parents':3,'result_parents':len(signed(folder/'settings')),'screenshots':[p.name for p in folder.glob('*.png')]})
        write(out/'progress.json',results)
    report={'passed':True,'cases':results,'frozen_exe':bool(args.exe),'live_platform':False,'real_prepare_inspect_merge_apply':True,'dialogs_not_stubbed':True,'real_packaged_component_open_after_merge':bool(args.exe),'queued_machine_dispatch_count':0}
    if args.exe:report['exe_sha256']=hash_file(args.exe)
    write(out/'report.json',report);print(json.dumps({'passed':True,'cases':len(results),'report':str(out/'report.json')},ensure_ascii=False))


if __name__=='__main__':
    import multiprocessing
    multiprocessing.freeze_support();main()
