"""Seal a distinct verified build; never consume historical QA implicitly."""
import argparse,hashlib,json,shutil,subprocess,zipfile
from pathlib import Path
root=Path(__file__).resolve().parent
parser=argparse.ArgumentParser()
parser.add_argument('--delivery',type=Path,required=True)
parser.add_argument('--budget-report',type=Path,required=True)
parser.add_argument('--batch-report',type=Path,required=True)
parser.add_argument('--ui-report',type=Path,required=True)
args=parser.parse_args();dest=args.delivery.resolve()
digest=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
meta=json.loads((dest/'版本信息.json').read_text(encoding='utf-8'))
commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip()
assert commit==meta['source_commit'],'Commit differs from packaged source'
assert not subprocess.check_output(['git','status','--porcelain'],cwd=root,text=True).strip(),'Commit pending source changes first'
evidence={}
for name,path in [('budget',args.budget_report),('batch',args.batch_report),('packaged_ui',args.ui_report)]:
    data=json.loads(path.read_text(encoding='utf-8'))
    assert data['passed'] and data['version']==meta['version'] and data['source_commit']==commit,(name,'stale or failed evidence')
    if name=='packaged_ui':assert data['exe_sha256']==digest(dest/meta['entry'])
    evidence[name]=data
assert not (dest/'_internal/icuuc.dll').exists()
out=root/'releases'/meta['version']/meta['build_id'];out.mkdir(parents=True,exist_ok=False)
notes={'version':meta['version'],'source_commit':commit,'build_id':meta['build_id'],'verified':['business budget regression','single/multi-file grouping and conflicts','serial vs process concurrency equality','budget isolation and failures','packaged EXE interactions and exports'],'not_verified':['other computers','byte-reproducible rebuild'],'rules':'Original 0.3 rules unchanged; no daily-rate unification or experimental protection period'}
(dest/'验收记录.json').write_text(json.dumps(notes,ensure_ascii=False,indent=2),encoding='utf-8')
files=sorted(p for p in dest.rglob('*') if p.is_file());manifest={str(p.relative_to(dest)):digest(p) for p in files}
archive=dest.parent/f"补货助手_{meta['version']}_{meta['build_id']}.zip"
with zipfile.ZipFile(archive,'x',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as z:
    for p in files:z.write(p,p.relative_to(dest.parent))
with zipfile.ZipFile(archive) as z:assert z.testzip() is None
for name,data in evidence.items():(out/(name+'_report.json')).write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
for name in ['版本信息.json','验收记录.json','使用说明.txt']:shutil.copy2(dest/name,out/name)
(out/'delivery_sha256.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
report={**meta,'archive':str(archive),'zip_sha256':digest(archive),'exe_sha256':digest(dest/meta['entry']),'file_count':len(files),'zip_bytes':archive.stat().st_size}
(out/'release_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(report,ensure_ascii=False,indent=2))
