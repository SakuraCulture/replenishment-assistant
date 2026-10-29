"""Actual packaged app, isolated settings, tracked descendants and no-orphan check."""
import argparse,hashlib,json,os,subprocess,sys,time
from pathlib import Path
root=Path(__file__).resolve().parent;sys.path.insert(0,str(root/'tools'))
from windows_process_metrics import sample_tree,processes
p=argparse.ArgumentParser();p.add_argument('--exe',type=Path,required=True);p.add_argument('--qa',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);env=os.environ.copy()
for k in ['PYTHONPATH','PYTHONHOME','QT_QPA_PLATFORM','QT_PLUGIN_PATH']:env.pop(k,None)
env['PATH']=str(Path(env['WINDIR'])/'System32')+';'+env['WINDIR'];env['REPLENISHMENT_DATA_DIR']=str(out/'settings')
run=subprocess.Popen([str(a.exe.resolve()),'--intake-smoke',str(a.qa.resolve()),str(out)],env=env,cwd=out);seen=set();start=time.monotonic()
while run.poll() is None:
    seen.update(sample_tree(run.pid));assert time.monotonic()-start<240,'EXE smoke timeout; inspect running process before retry'
    time.sleep(.1)
assert run.returncode==0,run.returncode
remaining=seen & set(processes());assert not remaining,remaining
report=json.loads((out/'intake_ui_report.json').read_text(encoding='utf-8'));assert report['passed'],report
report.update(exe_sha256=hashlib.sha256(a.exe.read_bytes()).hexdigest(),observed_process_ids=sorted(seen),remaining_process_ids=sorted(remaining))
(out/'intake_ui_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(report,ensure_ascii=False,indent=2))
