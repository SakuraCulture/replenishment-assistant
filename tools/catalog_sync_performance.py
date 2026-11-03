"""Manual baseline versus auto validation/adoption, frozen real file; excludes platform/network time."""
import argparse,hashlib,json,subprocess,sys,time,statistics
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
p=argparse.ArgumentParser();p.add_argument('--qa',type=Path,required=True);p.add_argument('--child',choices=['manual','auto']);p.add_argument('--out',type=Path);a=p.parse_args();qa=a.qa.resolve();file=qa/'inputs/catalog-real.xlsx'
if a.child:
 source=qa/'baseline' if a.child=='manual' else ROOT;sys.path.insert(0,str(source/'src'));import work_repository as wr,warehouse_catalog as wc
 out=a.out;out.mkdir(parents=True,exist_ok=False);folder=out/'settings';db=wr.Repository(folder);start=time.perf_counter()
 if a.child=='manual':
  import source_archive
  c=wc.import_catalog(file,'山东仓');source_archive.capture(db.path,c['source_path'],c['source_sha256']);wc.Repository(folder).save(c)
 else:
  import shutil,catalog_sync_validation as v,catalog_sync as s
  directory=out/'node/downloads/b/i';directory.mkdir(parents=True);target=directory/'source.xlsx';shutil.copyfile(file,target)
  receipt=v.validate({'schema':1,'file':str(target),'warehouse':'山东仓','sha256':v.digest(target),'expected_rows':9207,'candidate':str(directory/'candidate.json')});item={'id':'i','receipt':receipt,'account':{'warehouse':'山东仓','expectedCoid':'1','expectedUid':'2'},'native':{'sha256':receipt['source_sha256'],'scope':{'coid':'1','uid':'2'}},'expected_catalog_digest':None}
  s.Adoptions(folder,db.path,out/'node').apply({'id':'b','purpose':'default_catalog'},item);c=wc.Repository(folder).catalogs()['山东仓']
 elapsed=time.perf_counter()-start;business=hashlib.sha256(json.dumps(c['items'],ensure_ascii=False,sort_keys=False).encode()).hexdigest();(out/'result.json').write_text(json.dumps({'seconds':elapsed,'rows':len(c['items']),'ordered_items_sha256':business,'source_sha256':c['source_sha256']}),'utf-8');sys.exit(0)
results={'manual':[],'auto':[]}
for trial in range(3):
 for mode in (['manual','auto'] if trial%2==0 else ['auto','manual']):
  out=qa/'performance'/f'{trial}-{mode}';subprocess.run([sys.executable,__file__,'--qa',str(qa),'--child',mode,'--out',str(out)],check=True);results[mode].append(json.loads((out/'result.json').read_text('utf-8')))
report={'scope':'real frozen 9207-row file; local read+original preservation+adoption, excludes browser/query/download; auto includes staging copy; same machine/Python, serial AB BA AB, warm OS cache not purged','raw':results,'summary':{k:{'min':min(r['seconds'] for r in rows),'median':statistics.median(r['seconds'] for r in rows),'max':max(r['seconds'] for r in rows)} for k,rows in results.items()},'ordered_items_equal':len({r['ordered_items_sha256'] for rows in results.values() for r in rows})==1,'originals_equal':len({r['source_sha256'] for rows in results.values() for r in rows})==1}
(qa/'performance/report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8');print(json.dumps(report,ensure_ascii=False))
