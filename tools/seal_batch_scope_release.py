"""Seal only the tested 0.33.0 rc1 bundle; never overwrite an existing ZIP."""
import hashlib,json,shutil,subprocess,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def save(p,v):p.write_text(json.dumps(v,ensure_ascii=False,indent=2),encoding='utf-8')
def main():
    qa=ROOT/'qa/v0330';release=ROOT/'releases/0.33.0/rc1';release.mkdir(parents=True,exist_ok=True)
    delivery=ROOT/'交付_0.33.0_rc1/补货助手';build=json.loads((delivery/'_internal/build_info.json').read_text('utf-8'))
    exe=json.loads((qa/'exe/batch-exe-report.json').read_text('utf-8'));comparison=json.loads((qa/'comparison-serial/report.json').read_text('utf-8'))
    if not exe['passed'] or not exe['frozen_exe'] or exe['source_commit']!=build['source_commit']:raise ValueError('Actual EXE acceptance/commit mismatch')
    if not comparison['passed']:raise ValueError('Comparison not passed')
    if 'Ran 692 tests' not in (qa/'all-tests-final.txt').read_text('utf-8',errors='replace') or not (qa/'all-tests-final.txt').read_text('utf-8',errors='replace').strip().endswith('OK'):raise ValueError('Regression evidence missing')
    protected=json.loads((qa/'protected-files.json').read_text('utf-8'))
    for name,value in protected.items():
        value['after']=sha(ROOT/name)
        expected=value.get('baseline',value.get('before'))
        if value['after']!=expected:raise ValueError('Protected file changed: '+name)
    save(qa/'protected-files.json',protected)
    for name in ['all-tests-final.txt','affected-final.txt','backend-final.txt','environment.json','baseline_manifest.json','protected-files.json']:
        shutil.copy2(qa/name,release/name)
    for src,name in [('comparison-serial/report.json','comparison.json'),('source-final/batch-exe-report.json','source-ui-report.json'),('exe/batch-exe-report.json','exe-report.json'),('exe/01-暂停批次.png','01-暂停批次.png'),('exe/02-批次结果.png','02-批次结果.png')]:shutil.copy2(qa/src,release/name)
    files=sorted(p for p in delivery.rglob('*') if p.is_file())
    manifest={p.relative_to(delivery).as_posix():sha(p) for p in files}
    save(release/'delivery-manifest.json',{'source_commit':build['source_commit'],'files':manifest})
    archive=delivery.parent/'补货助手_0.33.0_rc1_批次库存与范围.zip'
    with zipfile.ZipFile(archive,'x',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for p in files:z.write(p,'补货助手/'+p.relative_to(delivery).as_posix())
    with zipfile.ZipFile(archive) as z:
        if z.testzip() is not None:raise ValueError('ZIP CRC failed')
        if len(z.namelist())!=len(manifest):raise ValueError('ZIP count mismatch')
        for name,h in manifest.items():
            if hashlib.sha256(z.read('补货助手/'+name)).hexdigest()!=h:raise ValueError('ZIP hash mismatch: '+name)
    record={'source_commit':build['source_commit'],'version':'0.33.0','build_id':'rc1','file_count':len(files),'exe_sha256':sha(delivery/'补货助手.exe'),'zip_sha256':sha(archive),'zip':str(archive),'delivery':str(delivery),'crc_and_per_file_sha256':'passed','legacy_exe_unchanged':True}
    save(release/'package.json',record);print(json.dumps(record,ensure_ascii=False))
if __name__=='__main__':main()
