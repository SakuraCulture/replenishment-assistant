"""Run existing real Qt-button acceptance against the immutable independent EXE."""
import hashlib,json,os,subprocess,sys,traceback
from pathlib import Path
Q=Path(__file__).resolve().parent;ROOT=Path.cwd();OUT=Q.parents[1]
EXE=OUT/'交付_0.54.6_rc1/补货助手/补货助手.exe'

def read(p):return json.loads(Path(p).read_text('utf-8'))
def write(p,v):Path(p).write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n','utf-8')

def main():
 result={'passed':False,'actual_exe':True,'synthetic':True,'live_platform':False,'runs':[]}
 try:
  assert read(Q/'comparison.json')['passed'] and read(Q/'regression-final.json')['passed']
  assert read(Q/'build-report-rc1.json')['passed'] and EXE.is_file()
  with EXE.open('rb') as f:result['exe_sha256']=hashlib.file_digest(f,'sha256').hexdigest()
  assert result['exe_sha256']==read(Q/'build-report-rc1.json')['exe_sha256']
  env=os.environ.copy();env.update(PYTHONUTF8='1',PYTHONPATH=os.pathsep.join(str(ROOT/p) for p in ('src','tests','tools')))
  env.pop('PYTHONHOME',None)
  for key,part in [('LOCALAPPDATA','local'),('APPDATA','roaming'),('TEMP','temp'),('TMP','temp')]:
   p=Q/'native-driver-environment'/part;p.mkdir(parents=True,exist_ok=True);env[key]=str(p)
  for name,script,timeout in [('analytics','verify_analytics_templates.py',240),('lifecycle','verify_lifecycle_performance.py',300)]:
   # Read-only preflight. The application retains its own instance/DB guards.
   cmd="Get-CimInstance Win32_Process -Filter \"Name='补货助手.exe'\" | Select-Object ProcessId,ExecutablePath | ConvertTo-Json -Compress"
   active=subprocess.check_output(['powershell.exe','-NoProfile','-Command',cmd],text=True,encoding='utf-8',errors='replace').strip()
   if active:raise RuntimeError('A replenishment application is running; wait for its normal safe closure: '+active)
   out=Q/('native-'+name+'-01')
   with (Q/('native-'+name+'-driver-01.log')).open('wb') as log:
    p=subprocess.Popen([sys.executable,str(ROOT/'tools'/script),'--out',str(out),'--exe',str(EXE),'--timeout',str(timeout)],cwd=ROOT,env=env,stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW)
    write(Q/'native-status.json',{'stage':name,'driver_pid':p.pid,'exe':str(EXE)});code=p.wait()
   receipt=read(out/'report.json') if (out/'report.json').is_file() else {'passed':False,'error':'Missing real EXE receipt'}
   result['runs'].append({'name':name,'exit_code':code,'report':str(out/'report.json'),'receipt':receipt})
   write(Q/'native-result.json',result)
   assert code==0 and receipt['passed'] and receipt['exe_sha256']==result['exe_sha256'],name
   assert len(receipt['runs'])==2 and all(r['passed'] and r['exit_code']==0 and r['frozen_exe'] and r['dialogs_not_stubbed'] for r in receipt['runs'])
   if name=='analytics':assert all(r['version']['version']=='0.54.6' and r['version']['build_id']=='rc1' for r in receipt['runs'])
   print(json.dumps({'native_passed':name,'checks':sum(len(r['checks']) for r in receipt['runs'])},ensure_ascii=False),flush=True)
  result.update(passed=True,checks=sum(len(r['checks']) for item in result['runs'] for r in item['receipt']['runs']))
  write(Q/'native-result.json',result);write(Q/'native-status.json',{'stage':'complete','passed':True,'checks':result['checks']})
 except BaseException:
  result['error']=traceback.format_exc();write(Q/'native-result.json',result);write(Q/'native-status.json',{'stage':'failed','error':result['error']});raise
if __name__=='__main__':main()
