"""Seal a scoped local candidate; live browser approval is explicitly pending."""
import argparse,hashlib,json,re,shutil,subprocess,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def read(p):return json.loads(Path(p).read_text('utf-8-sig'))
def sha(p):
 with Path(p).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def require(ok,text):
 if not ok:raise ValueError(text)
p=argparse.ArgumentParser();p.add_argument('--qa',type=Path,required=True);p.add_argument('--delivery',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();qa=a.qa.resolve();delivery=a.delivery.resolve();out=a.output.resolve()
meta=read(delivery/'版本信息.json');commit=meta['source_commit'];exe=sha(delivery/'补货助手.exe');eq=read(qa/'equivalence.json');ui=read(qa/'exe-final/report.json');old_ui=read(qa/'exe-lifecycle/comparison-report.json');frozen=read(qa/'baseline.json');perf=read(qa/'performance/report.json')
require(meta['version']=='0.31.0' and meta['model_version']=='0.3.4-rules','Wrong candidate')
require(eq['passed'] and eq['differences']==0,'Business comparison failed')
require(ui['passed'] and ui['frozen'] and ui['exe_sha256']==exe and ui['process_exit']==0 and ui['version']['source_commit']==commit,'JST actual EXE mismatch')
require(old_ui['passed'] and old_ui['frozen_exe'] and old_ui['exe_sha256']==exe and old_ui['source_commit']==commit and old_ui['exit_code']==0,'Lifecycle actual EXE mismatch')
require(read(qa/'environment.json')['passed'],'Environment failed')
real=read(qa/'exe-real-final/report.json');require(real['passed'] and not real['qt_imported'] and real['exe_sha256']==exe,'Real EXE file validation failed')
require(re.search(r'Ran 593 tests.*?\nOK\s*$',(qa/'all-python-final.txt').read_text('utf-8-sig'),re.S),'Full regressions failed')
require(re.search(r'pass 18\s*\n.*fail 0',(qa/'node-final-build.txt').read_text('utf-8-sig')),'Node regressions failed')
for name,digest in eq['source_hashes'].items():
 require(sha(ROOT/name)==digest and hashlib.sha256(subprocess.check_output(['git','show',commit+':'+name.replace('\\','/')],cwd=ROOT)).hexdigest()==digest,'Source not frozen: '+name)
require(all(sha(p)==h for p,h in frozen['protected'].items()),'Protected file changed')
require(all(sha(ROOT/p)==h for p,h in frozen['aoxiang_source'].items()),'AX source changed')
# Add only reviewed documentation to this new candidate. No overlay of runtime bytes.
(delivery/'docs').mkdir(exist_ok=True)
for name in ['0.31.0聚水潭商品同步试用.md','聚水潭商品同步实施记录_0.31.0.md']:
 shutil.copy2(ROOT/'docs'/name,delivery/'docs'/name)
shutil.copy2(ROOT/'docs/0.31.0聚水潭商品同步试用.md',delivery/'使用说明.txt')
shutil.copy2(ROOT/'requirements-win-amd64.lock',delivery/'requirements-win-amd64.lock')
source=ROOT/('build_0.31.0_'+meta['build_id'])/'source';runtime_files={p.relative_to(delivery).as_posix():sha(p) for p in delivery.rglob('*') if p.is_file()}
out.mkdir(parents=True,exist_ok=False)
reports={'equivalence.json':qa/'equivalence.json','exe-sync.json':qa/'exe-final/report.json','exe-lifecycle.json':qa/'exe-lifecycle/comparison-report.json','performance.json':qa/'performance/report.json','python-tests.txt':qa/'all-python-final.txt','node-tests.txt':qa/'node-final-build.txt','environment.json':qa/'environment.json','exe-real-catalog.json':qa/'exe-real-final/report.json'}
for name,path in reports.items():shutil.copy2(path,out/name)
screens=out/'screenshots';screens.mkdir()
for path in (qa/'exe-final').glob('*.png'):shutil.copy2(path,screens/path.name)
zip_path=delivery.parent/'补货助手_0.31.0_聚水潭同步候选.zip'
with zipfile.ZipFile(zip_path,'x',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
 for name in runtime_files:z.write(delivery/name,'补货助手/'+name)
with zipfile.ZipFile(zip_path) as z:
 require(set(z.namelist())=={'补货助手/'+n for n in runtime_files},'ZIP members mismatch')
 for name,h in runtime_files.items():require(hashlib.sha256(z.read('补货助手/'+name)).hexdigest()==h,'ZIP checksum mismatch')
report={'status':'LOCAL_CANDIDATE_LIVE_AUTH_PENDING','version':'0.31.0','baseline':frozen['baseline'],'source_commit':commit,'branch':subprocess.check_output(['git','branch','--show-current'],cwd=ROOT,text=True).strip(),'exe_sha256':exe,'delivery':str(delivery),'zip':str(zip_path),'zip_sha256':sha(zip_path),'files':runtime_files,'protected_count':len(frozen['protected']),'protected_unchanged':True,'AX_source_unchanged':True,'equivalence_differences':eq['differences'],'live':'Existing Linyi dedicated Edge connection authorization timed out; no platform export submitted; second account explicitly deferred; physical warehouse mapping unconfirmed','model_rules_changed':False,'tests':{'python':593,'node':18,'exe_sync_checks':len(ui['checks'])},'performance':perf['summary']}
(out/'release.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8');print(json.dumps({k:v for k,v in report.items() if k!='files'},ensure_ascii=False))
