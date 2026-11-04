"""Record the tested 0.35.0 candidate; no remote publishing or old package overwrite."""
import hashlib,json,shutil,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def sha(p):
    with p.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def save(p,x):p.write_text(json.dumps(x,ensure_ascii=False,indent=2),'utf-8')
def main():
    qa=ROOT/'qa/v0350';release=ROOT/'releases/0.35.0/rc1';release.mkdir(parents=True,exist_ok=True)
    delivery=ROOT/'交付_0.35.0_rc1/补货助手';build=json.loads((delivery/'_internal/build_info.json').read_text('utf-8'))
    acceptance=json.loads((qa/'exe/automation-report.json').read_text('utf-8'))
    tested=json.loads((qa/'exe/delivery-evidence.json').read_text('utf-8'))
    eq=json.loads((ROOT/'qa/v0350-locked/equivalence.json').read_text('utf-8'))
    perf=json.loads((qa/'performance-isolated/performance.json').read_text('utf-8'))
    if not all([acceptance['passed'],acceptance['frozen_exe'],tested['unchanged'],eq['passed'],perf['passed']]):raise ValueError('Missing passing acceptance')
    if tested['build']!=build or any(sha(Path(p))!=h for p,h in tested['hashes'].items()):raise ValueError('Tested package changed')
    baseline=json.loads((ROOT/'qa/v0350-locked/baseline.json').read_text('utf-8'))
    if any(sha(Path(p))!=h for p,h in baseline['protected'].items()):raise ValueError('Protected baseline changed')
    checks=['locked-unit-tests.log','gateway-final.log']
    for n in checks:
        if not (qa/n).read_text('utf-8',errors='replace').strip().endswith('OK'):raise ValueError('Tests not passing: '+n)
        shutil.copy2(qa/n,release/n)
    for source,name in [('exe/automation-report.json','exe-report.json'),('exe/delivery-evidence.json','build-evidence.json'),
        ('exe/codex-client/report.json','codex-client.json'),('exe/AI协助工作台.png','01-AI协助工作台.png'),('exe/AI任务结果.png','02-AI任务结果.png'),
        ('performance-isolated/performance.json','performance.json'),('locked-environment.json','environment.json')]:shutil.copy2(qa/source,release/name)
    for n in ('baseline.json','equivalence.json'):shutil.copy2(ROOT/'qa/v0350-locked'/n,release/n)
    files=sorted(p for p in delivery.rglob('*') if p.is_file());manifest={p.relative_to(delivery).as_posix():sha(p) for p in files}
    save(release/'delivery-manifest.json',{'source_commit':build['source_commit'],'files':manifest})
    archive=delivery.parent/'补货助手_0.35.0_rc1_AI协助.zip'
    with zipfile.ZipFile(archive,'x',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for p in files:z.write(p,'补货助手/'+p.relative_to(delivery).as_posix())
    with zipfile.ZipFile(archive) as z:
        if z.testzip() is not None or len(z.namelist())!=len(files):raise ValueError('ZIP integrity failed')
        for n,h in manifest.items():
            if hashlib.sha256(z.read('补货助手/'+n)).hexdigest()!=h:raise ValueError('ZIP member hash failed: '+n)
    package={'source_commit':build['source_commit'],'version':'0.35.0','model':'0.3.4-rules','build_id':'rc1','file_count':len(files),
        'exe_sha256':sha(delivery/'补货助手.exe'),'bridge_sha256':sha(delivery/'AI接口/补货助手AI接口.exe'),
        'zip_sha256':sha(archive),'zip':str(archive),'delivery':str(delivery),'crc_and_per_file_sha256':'passed','protected_baseline_unchanged':True}
    save(release/'package.json',package);print(json.dumps(package,ensure_ascii=False))
if __name__=='__main__':main()
