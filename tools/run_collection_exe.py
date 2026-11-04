"""Launch the actual GUI build, require clean process exit as well as UI assertions."""
import argparse,json,os,subprocess,sys
from pathlib import Path
from prepare_collection_maintenance import seed
def main():
    p=argparse.ArgumentParser();p.add_argument('--exe',type=Path,required=True);p.add_argument('--out',type=Path,required=True);a=p.parse_args()
    out=a.out.resolve();seed(out)
    env=os.environ.copy();env.pop('QT_QPA_PLATFORM',None);env['REPLENISHMENT_DATA_DIR']=str(out/'settings');env['PYTHONUTF8']='1'
    result=subprocess.run([str(a.exe.resolve()),'--collection-maintenance-smoke',str(out)],env=env,timeout=90,creationflags=subprocess.CREATE_NO_WINDOW)
    report=json.loads((out/'exe-report.json').read_text('utf-8'));report['process_exit_code']=result.returncode
    report['accepted']=report.get('passed',False) and result.returncode==0 and report.get('shared_component_closed',False)
    (out/'process-acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(report,ensure_ascii=False))
    if not report['accepted']:raise AssertionError('Actual EXE acceptance failed')
if __name__=='__main__':main()
