import hashlib,json,os,shutil,subprocess,sys,time
from pathlib import Path
from contextlib import closing
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT/'src'))
import payment_matching as pm
QA=ROOT/'qa/payment_auto_0491';out=QA/'exe-samples1';out.mkdir(exist_ok=False);settings=out/'settings';settings.mkdir()
shutil.copyfile(QA/'samples-final/settings/workbench.sqlite3',settings/'workbench.sqlite3')
index=pm.build(settings/'workbench.sqlite3');sd=next(h for h in index['heads'] if h['store']=='TBBLHDYJ20260617' and h['warehouse']=='山东仓');hb=next(h for h in index['heads'] if h['store']==sd['store'] and h['warehouse']=='河北仓')
source=ROOT.parent/'付款认单案例_20261108_02/付款文件';files={p.name[:2]:p for p in source.glob('*.xlsx') if not p.name.startswith('~$')}
cases=[]
for key in ('02','03','04','05','06','07'):
    path=QA/'samples-final/03_原始未填签发号.xlsx' if key=='03' else files[key]
    cases.append({'name':key,'path':str(path),'expected_number':(hb if key=='02' else sd)['number'] if int(key)<=5 else None,'reason':'少 1 款' if int(key)>5 else '', 'screenshot':f'真实案例_{key}.png'})
cases.append({'name':'03错误签发号','path':str(files['03']),'expected_number':None,'reason':'冲突','screenshot':'真实案例_03错误签发号.png'})
(out/'matching-fixture.json').write_text(json.dumps({'head':sd,'payment':str(files['01']),'automatic_cases':cases,'synthetic':False,'live_platform':False},ensure_ascii=False),'utf-8')
exe=ROOT/'交付_0.49.1_rc1/补货助手/补货助手.exe'
env={**os.environ,'REPLENISHMENT_DATA_DIR':str(settings),'LOCALAPPDATA':str(out/'localappdata'),'PYTHONUTF8':'1','QT_QPA_PLATFORM':'windows'}
with (out/'process.log').open('wb') as log:
    proc=subprocess.Popen([str(exe),'--payment-matching-smoke',str(out)],cwd=ROOT,env=env,stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW);start=time.monotonic()
    while proc.poll() is None:
        if time.monotonic()-start>300:raise TimeoutError(f'Isolated process {proc.pid} retained; not terminated')
        time.sleep(.2)
report=json.loads((out/'report.json').read_text('utf-8'));report['process_exit']=proc.returncode
with exe.open('rb') as f:report['exe_sha256']=hashlib.file_digest(f,'sha256').hexdigest()
report['frozen_order_copy_only']=True;report['live_database_written']=False
(out/'acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8');print(json.dumps(report,ensure_ascii=False));assert report['passed'] and proc.returncode==0
