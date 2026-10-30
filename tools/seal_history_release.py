"""Seal 0.7.2 only after current-commit equivalence and real EXE acceptance."""
import argparse,hashlib,json,subprocess,shutil,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
p=argparse.ArgumentParser();p.add_argument('--delivery',type=Path,required=True);p.add_argument('--qa',type=Path,required=True);a=p.parse_args();dest=a.delivery.resolve();qa=a.qa.resolve();meta=json.loads((dest/'版本信息.json').read_text(encoding='utf-8'));commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip();assert meta['version']=='0.7.2' and meta['source_commit']==commit;assert not subprocess.check_output(['git','status','--porcelain'],cwd=ROOT,text=True).strip()
paths={'equivalence':qa/'candidate/equivalence.json','normal':qa/'normal/normal_report.json','history_ui':qa/'exe-history/history_ui_report.json','normal_ui':qa/'exe-normal/normal_ui_report.json','delivery_ui':qa/'exe-delivery/export_ui_report.json','work_ui':qa/'exe-work/work_ui_report.json'};evidence={}
for name,path in paths.items():
    d=json.loads(path.read_text(encoding='utf-8'));assert d['passed'] and d['source_commit']==commit,(name,'failed/stale commit');assert not d.get('development');evidence[name]=d
    if name.endswith('_ui'):assert d['exe_sha256']==sha(dest/'补货助手.exe')
assert evidence['equivalence']['differences']==0 and evidence['equivalence']['strict_order']
assert evidence['equivalence']['candidate_sources']=={str(p.relative_to(ROOT)):sha(p) for p in sorted((ROOT/'src').glob('*')) if p.is_file()}
perf=json.loads((qa/'bench-loop/report.json').read_text());assert perf['rounds']==3
order_hashes={r['visible_order_sha256'] for r in perf['runs']};assert len(order_hashes)==1
protected=json.loads((qa/'baseline_manifest.json').read_text());unchanged={k:sha(ROOT/k)==v for k,v in protected['files'].items()};assert all(unchanged.values());assert sha(ROOT/'交付_0.7.1_rc2/补货助手/补货助手.exe')==protected['old_exe'];assert sha(ROOT/'交付_0.7.1_rc2/补货助手_0.7.1_rc2.zip')==protected['old_zip']
user=Path.home()/'AppData/Local/YoutuanTools/补货助手/settings/workbench.sqlite3';assert sha(user)==protected['original_db']
out=ROOT/'releases/0.7.2'/meta['build_id'];out.mkdir(parents=True,exist_ok=False)
for name,path in paths.items():shutil.copy2(path,out/(name+'.json'))
for name,path in {'performance':qa/'bench-loop/report.json','stage_a':qa/'stage_a.json','stage_b':qa/'stage_b_final/report.json','stage_c':qa/'stage_c.json','merchant':qa/'merchant/report.json','boundaries':qa/'boundaries/report.json'}.items():
    d=json.loads(path.read_text(encoding='utf-8'));assert d.get('passed',True);shutil.copy2(path,out/(name+'.json'))
receipt={'version':'0.7.2','source_commit':commit,'build_id':meta['build_id'],'baseline':'a831049a78e41f33369ce06b28709f3080324087','passed':True,'unchanged':unchanged,'user_db_unchanged':True,'old_delivery_unchanged':True,'exe_sha256':sha(dest/'补货助手.exe'),'limits':['daily storage and full large-job restore unchanged','first index build still loads complete snapshots in background','warm view timings include derived-cache reuse','same-machine validation; not a clean-machine reproducible build'],'not_pushed_or_merged':True}
(dest/'验收记录.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8');files={p.relative_to(dest).as_posix():sha(p) for p in dest.rglob('*') if p.is_file()};(out/'files_sha256.json').write_text(json.dumps(files,ensure_ascii=False,indent=2),encoding='utf-8')
archive=dest.parent/('补货助手_0.7.2_'+meta['build_id']+'.zip')
with zipfile.ZipFile(archive,'x',zipfile.ZIP_DEFLATED) as z:
    for rel in files:z.write(dest/rel,arcname=dest.name+'/'+rel)
receipt['zip_sha256']=sha(archive);receipt['zip']=str(archive);receipt['delivery']=str(dest);(out/'release_report.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(receipt,ensure_ascii=False))
