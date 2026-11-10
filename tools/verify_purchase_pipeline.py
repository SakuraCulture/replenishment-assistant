"""New-directory-only offline source/EXE acceptance. No platform or business data."""
import argparse,hashlib,json,os,subprocess,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tools')]


def main():
    p=argparse.ArgumentParser();p.add_argument('--exe',type=Path);p.add_argument('--out',type=Path,required=True);a=p.parse_args()
    out=a.out.resolve();out.relative_to(ROOT/'qa');out.mkdir(parents=True,exist_ok=False)
    from prepare_aoxiang_exe_fixture import prepare as fixture
    from aoxiang_purchase_tasks import prepare
    from aoxiang_client import ProcessClient
    from aoxiang_directory import atomic_json
    component=ROOT/'components/aoxiang';f=fixture(out,component)
    client=ProcessClient(component,out/'settings/aoxiang/component-state')
    try:
        client.start();config=client.request('configuration')
        package=prepare(out/'settings/workbench.sqlite3',f['head'],config,{'河北仓':'2078246','山东仓':'ZXC2025'},f['receipt'],out/'settings/aoxiang/purchase-packages',component/'assets/purchase-import-template-contract.json')
        atomic_json(Path(package['package_file']).parent/'本地任务索引.json',package)
        client.request('purchase.reserve',package_file=package['package_file'],package_sha256=package['package_sha256'])
    finally:
        if not client.close(timeout=10):raise RuntimeError('Offline fixture component has not closed safely')
    # Synthetic interruption only; no browser adapter is constructed here.
    script="""import {StateStore} from './src/state.mjs';import {PurchaseCreates} from './src/purchase-create-ledger.mjs';
const state=await StateStore.open(process.argv[1]),ledger=await PurchaseCreates.open(state),id=process.argv[2];
let r=ledger.get(id);for(const phase of ['PREPARING','READY','SUBMITTING','NEEDS_REVIEW'])r=ledger.update(id,r.sequence,phase,phase==='SUBMITTING'?{uploadAttempted:true}:phase==='NEEDS_REVIEW'?{error:{code:'SUBMISSION_UNKNOWN',message:'SYNTHETIC offline interruption; no platform request'}}:{});state.close();"""
    subprocess.run([str(component/'runtime/node.exe'),'--input-type=module','-e',script,str(out/'settings/aoxiang/component-state'),package['id']],cwd=component,check=True,capture_output=True)
    atomic_json(out/'pipeline-fixture.json',{**f,'package':package})
    env={**os.environ,'REPLENISHMENT_DATA_DIR':str(out/'settings'),'LOCALAPPDATA':str(out/'localappdata'),'PYTHONUTF8':'1'}
    for key in ('PYTHONHOME','QT_PLUGIN_PATH','QT_QPA_PLATFORM'):env.pop(key,None)
    command=[str(a.exe.resolve())] if a.exe else [sys.executable,str(ROOT/'src/app.py')]
    with (out/'process.log').open('w',encoding='utf-8') as log:
        process=subprocess.Popen(command+['--purchase-pipeline-smoke',str(out)],cwd=ROOT,env=env,stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW)
        began=time.monotonic()
        while process.poll() is None:
            if time.monotonic()-began>180:
                atomic_json(out/'still-running.json',{'pid':process.pid,'not_terminated':True})
                raise TimeoutError('Isolated EXE still active; inspect owned PID, not force killed')
            time.sleep(.2)
    report=json.loads((out/'exe-report.json').read_text('utf-8'));report['process_exit']=process.returncode
    if a.exe:
        with a.exe.open('rb') as stream:report['exe_sha256']=hashlib.file_digest(stream,'sha256').hexdigest()
    report['accepted']=report['passed'] and process.returncode==0
    atomic_json(out/'process-acceptance.json',report)
    print(json.dumps(report,ensure_ascii=False));return 0 if report['accepted'] else 1


if __name__=='__main__':sys.exit(main())
