"""New QA directory only; actual GUI + local component, never a live browser/platform."""
import argparse,hashlib,json,os,sqlite3,subprocess,sys
from contextlib import closing
from pathlib import Path
from prepare_collection_maintenance import seed
ROOT=Path(__file__).resolve().parents[1]


def main():
    p=argparse.ArgumentParser();p.add_argument('--exe',type=Path);p.add_argument('--out',type=Path,required=True);a=p.parse_args()
    out=a.out.resolve();out.relative_to(ROOT/'qa');fixture=seed(out)
    with closing(sqlite3.connect(Path(fixture['state_root'])/'state.sqlite')) as db,db:
        raw=db.execute("SELECT payload FROM attempts WHERE id='attempt-W2'").fetchone()[0];attempt=json.loads(raw)
        attempt.update(stage='SUBMISSION_UNKNOWN',errorCode='UI_ACTION_TIMEOUT')
        db.execute('UPDATE attempts SET stage=?,payload=? WHERE id=?',(attempt['stage'],json.dumps(attempt,ensure_ascii=False),'attempt-W2'))
    env={**os.environ,'REPLENISHMENT_DATA_DIR':str(out/'settings'),'LOCALAPPDATA':str(out/'localappdata'),'PYTHONUTF8':'1'}
    for key in ['QT_QPA_PLATFORM','QT_PLUGIN_PATH','PYTHONHOME']:env.pop(key,None)
    command=[str(a.exe.resolve())] if a.exe else [sys.executable,str(ROOT/'src/app.py')]
    with (out/'process.log').open('w',encoding='utf-8') as log:
        r=subprocess.run(command+['--collection-reexport-smoke',str(out)],cwd=ROOT,env=env,stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW,timeout=120)
    report=json.loads((out/'exe-report.json').read_text('utf-8'));report['process_exit_code']=r.returncode
    report['accepted']=report.get('passed',False) and r.returncode==0 and report.get('shared_component_closed',False)
    if a.exe:
        with a.exe.open('rb') as stream:report['exe_sha256']=hashlib.file_digest(stream,'sha256').hexdigest()
    (out/'process-acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8');print(json.dumps(report,ensure_ascii=False))
    return 0 if report['accepted'] else 1


if __name__=='__main__':sys.exit(main())
