"""Capture the received 0.3.0 baseline without executing the app or rebuilding it."""
import ast,hashlib,json,shutil
from pathlib import Path

root=Path(__file__).resolve().parents[1]
dest=root/'交付_0.3/补货助手'
qa=root/'qa/v03'
out=root/'releases/0.3.0'
def digest(p):return hashlib.sha256(p.read_bytes()).hexdigest()
expected=json.loads((qa/'delivery_sha256.json').read_text(encoding='utf-8'))
actual={str(p.relative_to(dest)):digest(p) for p in dest.rglob('*') if p.is_file()}
assert expected==actual,'Delivery does not match accepted manifest'
release=json.loads((qa/'release_report.json').read_text(encoding='utf-8'))
assert digest(Path(release['archive']))==release['zip_sha256']
assert digest(dest/'补货助手.exe')==release['exe_sha256']
for name in ['default_config.json','default_must_sell.json','app.ico']:
    assert digest(root/'src'/name)==digest(dest/'_internal'/name),name
sources=sorted([*root.glob('*.py'),*root.glob('*.ps1'),*root.glob('requirements*.txt'),*[p for p in (root/'src').rglob('*') if p.is_file() and '__pycache__' not in p.parts]])
for p in sources:
    if p.suffix=='.py':ast.parse(p.read_text(encoding='utf-8-sig'),filename=str(p))
out.mkdir(parents=True,exist_ok=True)
for name in ['delivery_sha256.json','release_report.json','backend_report.json']:
    shutil.copy2(qa/name,out/name)
for name in ['使用说明.txt','验收记录.txt','版本信息.json']:
    shutil.copy2(dest/name,out/name)
shutil.copy2(qa/'packaged_ui/ui_report.json',out/'packaged_ui_report.json')
report={'date':'2026-10-29','app_version':'0.3.0','baseline_kind':'received source snapshot, not a reproducible build attestation',
 'delivery_files_verified':len(actual),'archive_sha256_verified':True,'packaged_configs_and_icon_match_source':True,
 'python_syntax_passed':True,'business_ui_tests_rerun':False,
 'source_sha256':{p.relative_to(root).as_posix():digest(p) for p in sources}}
(out/'baseline_audit.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps({k:v for k,v in report.items() if k!='source_sha256'},ensure_ascii=False))
