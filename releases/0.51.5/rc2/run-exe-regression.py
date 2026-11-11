"""Sequential acceptance on one immutable EXE; no live platforms or forced kills."""
import json,os,subprocess,sys
from pathlib import Path
ROOT=Path.cwd().resolve();QA=ROOT/'qa/catalog_rebind_0515_20261111_02'
EXE=ROOT/'交付_0.51.5_rc2/补货助手/补货助手.exe'
env={**os.environ,'PYTHONUTF8':'1','LOCALAPPDATA':str(QA/'exe-runner-localappdata')}
cases=[
    ('catalog','tools/verify_catalog_sync_ui.py','--output',str(QA/'exe-catalog')),
    ('floor','tools/verify_first_fill.py','--out',str(QA/'exe-floor'),'--floor-comparison',str(ROOT/'qa/stockout_floor_fix_20261111_01/comparison-01/comparison.json')),
    ('analytics','tools/verify_analytics_identifiers.py','--out',str(QA/'exe-analytics')),
    ('preissue','tools/verify_preissue_exe.py','--output',str(QA/'exe-preissue')),
]
results=[]
for name,script,*args in cases:
    log=QA/('exe-'+name+'-run.txt');assert not log.exists()
    print('START '+name,flush=True)
    with log.open('wb') as out:
        r=subprocess.run([sys.executable,str(ROOT/script),'--exe',str(EXE),*args],cwd=ROOT,env=env,stdout=out,stderr=subprocess.STDOUT)
    if r.returncode:raise RuntimeError(name+' failed; see '+str(log))
    file=QA/('exe-'+name)/('acceptance.json' if name in ('floor','analytics') else 'report.json')
    result=json.loads(file.read_text('utf-8'));assert result['passed']
    results.append({'case':name,'report':str(file),'passed':True,'checks':sum(len(i['checks']) for i in result['runs']) if name=='preissue' else len(result['checks'])})
    print(json.dumps(results[-1]),flush=True)
(QA/'exe-regression.json').write_text(json.dumps({'passed':True,'runs':results,'live_platform':False},ensure_ascii=False,indent=2)+'\n','utf-8')
