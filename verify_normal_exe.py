"""Launch actual candidate EXE with private UI fixtures and user data."""
import argparse,hashlib,json,os,subprocess
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--exe',type=Path,required=True);p.add_argument('--fixtures',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--real-inputs',type=Path);a=p.parse_args()
out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);env=os.environ.copy()
for key in ['PYTHONPATH','PYTHONHOME','QT_QPA_PLATFORM','QT_PLUGIN_PATH']:env.pop(key,None)
env['PATH']=str(Path(env['WINDIR'])/'System32')+';'+env['WINDIR'];env['REPLENISHMENT_DATA_DIR']=str(out/'settings')
run=subprocess.run([str(a.exe.resolve()),'--normal-smoke',str(a.fixtures.resolve()),str(out)]+([str(a.real_inputs.resolve())] if a.real_inputs else []),cwd=out,env=env,timeout=300)
assert run.returncode==0,run.returncode
path=out/'normal_ui_report.json';report=json.loads(path.read_text(encoding='utf-8'));assert report['passed'],report
report['exe_sha256']=hashlib.sha256(a.exe.read_bytes()).hexdigest();path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(report,ensure_ascii=False,indent=2))
