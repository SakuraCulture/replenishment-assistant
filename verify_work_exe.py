"""Run packaged workbench UI checks in a private configuration directory."""
import argparse,hashlib,json,os,subprocess
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--exe',type=Path,required=True);p.add_argument('--fixtures',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
assert a.exe.is_file()
out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);env=os.environ.copy()
for k in ['PYTHONPATH','PYTHONHOME','QT_QPA_PLATFORM','QT_PLUGIN_PATH']:env.pop(k,None)
env['PATH']=str(Path(env['WINDIR'])/'System32')+';'+env['WINDIR'];env['REPLENISHMENT_DATA_DIR']=str(out/'settings')
run=subprocess.run([str(a.exe.resolve()),'--work-smoke',str(a.fixtures.resolve()),str(out)],cwd=out,env=env,timeout=240)
assert run.returncode==0,run.returncode
report=json.loads((out/'work_ui_report.json').read_text(encoding='utf-8'));assert report['passed'],report
report['exe_sha256']=hashlib.sha256(a.exe.read_bytes()).hexdigest();(out/'work_ui_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(report,ensure_ascii=False,indent=2))
