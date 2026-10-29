"""Explicit, isolated packaged application smoke test."""
import argparse,hashlib,json,os,subprocess
from pathlib import Path
parser=argparse.ArgumentParser();parser.add_argument('--exe',type=Path,required=True);parser.add_argument('--sales',type=Path,required=True);parser.add_argument('--products',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
args=parser.parse_args()
for p in [args.exe,args.sales,args.products]:assert p.is_file(),p
qa=args.output.resolve();qa.mkdir(parents=True,exist_ok=False)
env=os.environ.copy()
for k in ['PYTHONPATH','PYTHONHOME','QT_QPA_PLATFORM','QT_PLUGIN_PATH']:env.pop(k,None)
env['PATH']=str(Path(env['WINDIR'])/'System32')+';'+env['WINDIR'];env['REPLENISHMENT_DATA_DIR']=str(qa/'settings')
run=subprocess.run([str(args.exe.resolve()),'--batch-smoke',str(args.sales.resolve()),str(args.products.resolve()),str(qa)],env=env,cwd=qa,timeout=420)
assert run.returncode==0,run.returncode
report=json.loads((qa/'ui_report.json').read_text(encoding='utf-8'));assert report['passed'],report
report['exe_sha256']=hashlib.sha256(args.exe.read_bytes()).hexdigest()
(qa/'ui_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(report,ensure_ascii=False,indent=2))
