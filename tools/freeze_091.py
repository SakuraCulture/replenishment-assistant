"""Freeze private QA data and protect the delivered 0.9.0 package/user files."""
import hashlib,json,os,shutil,sqlite3
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];OUT=ROOT/'qa/inbound_091'
sha=lambda p:hashlib.sha256(Path(p).read_bytes()).hexdigest()
user=Path(os.environ['LOCALAPPDATA'])/'YoutuanTools/补货助手/settings'
protected={str(p):sha(p) for folder in [ROOT/'交付_0.9.0_rc2',user] for p in folder.rglob('*') if p.is_file()}
db=user/'workbench.sqlite3';target=OUT/'historical.sqlite3'
if target.exists():raise FileExistsError(target)
with sqlite3.connect(db.resolve().as_uri()+'?mode=ro',uri=True) as source,sqlite3.connect(target) as dest:source.backup(dest)
fixture=OUT/'fixture';fixture.mkdir()
for file in (ROOT/'qa/business_090/normal').glob('*.xlsx'):shutil.copy2(file,fixture/file.name)
import sys
sys.path.insert(0,str(ROOT/'src'));import work_repository as wr
p=wr.loads((ROOT/'qa/business_090/normal/payload.json').read_bytes())
# Explicit new-version synthetic fixture; values/expected quantities untouched.
p['model_version']='0.3.2-rules';(fixture/'payload.json').write_text(wr.dumps(p),encoding='utf-8')
trial=ROOT.parent/'inbound_floor_trial_20261030';inputs={str(f):sha(f) for f in [trial/'frozen.json',trial/'result.json',*sorted((trial/'frozen').glob('*.pklz'))]}
(OUT/'protected.json').write_text(json.dumps({'files':protected,'inputs':inputs,'baseline':'364929c69ed16d933d5dffaff7c2424323730af0','historical_copy':sha(target)},ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'protected_files':len(protected),'frozen_inputs':len(inputs),'snapshot':str(target)}))
