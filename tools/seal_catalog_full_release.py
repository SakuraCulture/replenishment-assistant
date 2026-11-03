"""Seal the dual-warehouse candidate only after matching EXE and source evidence.

Copies reviewed documentation/reports, never overlays runtime code. No upload.
"""
import argparse,hashlib,json,re,shutil,subprocess,sys,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
from catalog_sync_bundle import verify_component
def read(p):return json.loads(Path(p).read_text('utf-8-sig'))
def sha(p):
 with Path(p).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def require(ok,text):
 if not ok:raise ValueError(text)
def main():
 p=argparse.ArgumentParser();p.add_argument('--qa',type=Path,required=True);p.add_argument('--delivery',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();q=a.qa.resolve();d=a.delivery.resolve();out=a.output.resolve()
 require(d.is_relative_to(ROOT) and d.parent.name.startswith('交付_'),'Unexpected delivery target')
 require(not out.exists(),'Release record exists')
 meta=read(d/'版本信息.json');commit=meta['source_commit'];exe=sha(d/'补货助手.exe')
 eq=read(q/'equivalence.json');require(eq['passed'] and eq['differences']==0 and not eq['aoxiang_source_changed'],'Equivalence failed')
 require(meta['model_version']=='0.3.4-rules','Unexpected model')
 reports={'equivalence.json':q/'equivalence.json','chain.json':q/'chain-final/report.json','background.json':q/'background-final.json','environment.json':q/'environment-rc5.json','protected.json':q/'additional-protection-check.json'}
 for key,path in reports.items():require(read(path)['passed'],'Failed report '+key)
 for name,file in [('exe-sync','exe-final-rc5/report.json'),('exe-real-dual','exe-real-dual-rc5/report.json'),('exe-lifecycle','exe-lifecycle-rc5/comparison-report.json'),('exe-file-sd','exe-file-山东仓-rc5/report.json'),('exe-file-hb','exe-file-河北仓-rc5/report.json')]:
  report=read(q/file);require(report['passed'] and report['exe_sha256']==exe,'EXE report mismatch '+name)
  if name in ('exe-sync','exe-real-dual'):
   require(report['frozen'] and report['version']['source_commit']==commit,'Not matching frozen EXE '+name)
   require(any('100%' in c for c in report['checks']),'Missing final progress proof')
  if name=='exe-lifecycle':require(report['frozen_exe'] and report['source_commit']==commit,'Lifecycle EXE mismatch')
  reports[name+'.json']=q/file
 python=(q/'python-final.txt').read_text('utf-8-sig');pycount=re.search(r'Ran (\d+) tests.*?\nOK\s*$',python,re.S);require(pycount is not None,'Python suite failed')
 node=(q/'node-final.txt').read_text('utf-8-sig');nodecount=re.search(r'pass (\d+)',node);require(nodecount is not None and re.search(r'fail 0\s',node),'Component suite failed')
 frozen=read(q/'baseline.json');extra=read(q/'additional-protected.json')
 require(all(Path(s).is_file() and sha(s)==h for s,h in (frozen['protected']|extra).items()),'Protected data changed')
 require(all(sha(ROOT/s)==h for s,h in frozen['aoxiang_source'].items()),'AX code changed')
 manifest=verify_component(d/'_internal/components/jst_catalog',d/'_internal/components/aoxiang')
 require(manifest['source_commit']==commit,'Packaged component commit mismatch')
 for name,h in manifest['files'].items():
  original=subprocess.check_output(['git','show',commit+':components/jst_catalog/'+name],cwd=ROOT)
  require(hashlib.sha256(original).hexdigest()==h,'Packaged/source mismatch '+name)
 # Reviewed docs can advance after immutable build; runtime bytes cannot.
 (d/'docs').mkdir(exist_ok=True)
 for name in ['0.31.0聚水潭商品同步试用.md','聚水潭双仓完整验证_20261103.md','聚水潭商品同步实施记录_0.31.0.md']:shutil.copy2(ROOT/'docs'/name,d/'docs'/name)
 shutil.copy2(ROOT/'docs/0.31.0聚水潭商品同步试用.md',d/'使用说明.txt')
 out.mkdir(parents=True,exist_ok=False)
 reports.update({'python-tests.txt':q/'python-final.txt','node-tests.txt':q/'node-final.txt'})
 for name,path in reports.items():shutil.copy2(path,out/name)
 screens=out/'screenshots';screens.mkdir()
 for folder,prefix in [('exe-final-rc5','synthetic'),('exe-real-dual-rc5','real')]:
  for f in (q/folder).glob('*.png'):shutil.copy2(f,screens/(prefix+'-'+f.name))
 files={p.relative_to(d).as_posix():sha(p) for p in d.rglob('*') if p.is_file()}
 target=d.parent/'补货助手_0.31.0_rc5_双仓同步验证版.zip'
 with zipfile.ZipFile(target,'x',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
  for n in files:z.write(d/n,'补货助手/'+n)
 with zipfile.ZipFile(target) as z:
  require(set(z.namelist())=={'补货助手/'+n for n in files},'ZIP members differ')
  for n,h in files.items():require(hashlib.sha256(z.read('补货助手/'+n)).hexdigest()==h,'ZIP member differs')
 report={'status':'LOCAL_DUAL_WAREHOUSE_VERIFIED_CANDIDATE','source_commit':commit,'baseline':frozen['baseline'],'branch':subprocess.check_output(['git','branch','--show-current'],cwd=ROOT,text=True).strip(),'version':meta['version'],'build_id':meta['build_id'],'model_version':meta['model_version'],'exe_sha256':exe,'zip':str(target),'zip_sha256':sha(target),'delivery':str(d),'files':files,'tests':{'python':int(pycount[1]),'node':int(nodecount[1]),'equivalence_cases':len(eq['cases']),'equivalence_differences':0},'live':{'河北仓':{'task':'206073780','rows':4497},'山东仓':{'task':'206074295','rows':9207}},'limits':['Live native exports were run using source service; same immutable JS bytes verified in package. EXE tests replay actual downloaded originals, without new platform submission.','Synthetic lifecycle forms only in isolated QA database.','No new-PC or expired-login/CAPTCHA test. No credential persistence or unattended-login guarantee.'],'protected_checks':{'initial_files':len(frozen['protected']),'post_live_files':len(extra),'all_unchanged':True},'aoxiang_source_unchanged':True,'user_default_catalogs_replaced':False,'remote_push_or_merge':False}
 (out/'release.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
 print(json.dumps({k:v for k,v in report.items() if k!='files'},ensure_ascii=False))
if __name__=='__main__':main()
