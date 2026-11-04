"""Seal an independently verified 0.33.1 candidate; never overwrite old deliveries."""
import hashlib,json,shutil,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def sha(p):
    with p.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def save(p,value):p.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
def main():
    qa=ROOT/'qa/v0331';release=ROOT/'releases/0.33.1/rc2';release.mkdir(parents=True,exist_ok=True)
    delivery=ROOT/'交付_0.33.1_rc2/补货助手'
    build=json.loads((delivery/'_internal/build_info.json').read_text('utf-8'))
    exe=json.loads((qa/'exe-rc2/process-acceptance.json').read_text('utf-8'))
    if not exe['accepted'] or not exe['frozen_exe'] or exe['source_commit']!=build['source_commit']:raise ValueError('Actual EXE acceptance/commit mismatch')
    py=(qa/'all-python-final.txt').read_text('utf-8');node=(qa/'all-node-final.txt').read_text('utf-8')
    if 'Ran 696 tests' not in py or not py.rstrip().endswith('OK'):raise ValueError('Python regressions not passed')
    if 'pass 163' not in node or 'fail 0' not in node:raise ValueError('Node regressions not passed')
    baseline=json.loads((qa/'baseline.json').read_text('utf-8'));protected={}
    for name,before in baseline['hashes'].items():
        after=sha(ROOT/name);protected[name]={'before':before,'after':after}
        if before!=after:raise ValueError('Protected file changed: '+name)
    save(release/'protected-files.json',protected)
    for name in ['all-python-final.txt','all-node-final.txt','maintenance-python-final.txt','baseline.json']:
        shutil.copy2(qa/name,release/name)
    for source,target in [('exe-rc2/process-acceptance.json','exe-report.json'),('exe-rc2/01-结束入口.png','01-结束入口.png'),('exe-rc2/02-结束后可新建.png','02-结束后可新建.png'),('real-copy/report.json','real-copy-report.json')]:shutil.copy2(qa/source,release/target)
    files=sorted(p for p in delivery.rglob('*') if p.is_file());manifest={p.relative_to(delivery).as_posix():sha(p) for p in files}
    save(release/'delivery-manifest.json',{'build_commit':build['source_commit'],'files':manifest})
    archive=delivery.parent/'补货助手_0.33.1_rc2_采集任务维护.zip'
    with zipfile.ZipFile(archive,'x',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for p in files:z.write(p,'补货助手/'+p.relative_to(delivery).as_posix())
    with zipfile.ZipFile(archive) as z:
        if z.testzip() is not None or len(z.namelist())!=len(files):raise ValueError('Archive validation failed')
        for name,h in manifest.items():
            if hashlib.sha256(z.read('补货助手/'+name)).hexdigest()!=h:raise ValueError('Archive member differs')
    record={'version':'0.33.1','model_version':'0.3.4-rules','build_commit':build['source_commit'],'delivery':str(delivery),'zip':str(archive),'file_count':len(files),'exe_sha256':sha(delivery/'补货助手.exe'),'zip_sha256':sha(archive),'old_delivery_unchanged':True,'user_files_modified':False,'live_platform_export_tested':False}
    save(release/'package.json',record);print(json.dumps(record,ensure_ascii=False))
if __name__=='__main__':main()
