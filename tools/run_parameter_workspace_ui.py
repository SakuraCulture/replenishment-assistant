import argparse,json,os,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def main():
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);p.add_argument('--exe',type=Path);a=p.parse_args()
    out=a.out.resolve();out.mkdir(parents=True,exist_ok=False);(out/'synthetic.json').write_text('{"synthetic":true}',encoding='utf-8')
    env={**os.environ,'REPLENISHMENT_DATA_DIR':str(out/'settings'),'PYTHONUTF8':'1'};env.pop('QT_QPA_PLATFORM',None)
    cmd=[str(a.exe.resolve())] if a.exe else [sys.executable,str(ROOT/'src/app.py')]
    proc=subprocess.run([*cmd,'--parameter-workspace-smoke',str(out)],env=env,timeout=120,creationflags=subprocess.CREATE_NO_WINDOW)
    report=json.loads((out/'ui-report.json').read_text('utf-8'));report['process_exit_code']=proc.returncode;report['accepted']=report['passed'] and proc.returncode==0
    (out/'acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(report,ensure_ascii=False))
    if not report['accepted']:raise SystemExit(1)
if __name__=='__main__':main()
