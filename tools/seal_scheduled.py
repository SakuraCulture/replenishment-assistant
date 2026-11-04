"""Seal a local candidate only after fixed-source, component and actual EXE evidence.

Real platform/overnight acceptance is intentionally separate and cannot be inferred
from synthetic transports. This tool does not tag, push, merge or publish remotely.
"""
import hashlib,json,re,zipfile
from pathlib import Path
from structure_oracle import source_hashes
ROOT=Path(__file__).resolve().parents[1]
def sha(p):
    with Path(p).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def read(p):return json.loads(p.read_text('utf-8'))
def save(p,value):p.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n','utf-8',newline='\n')

def main():
    qa=ROOT/'qa/v0370';delivery=ROOT/'交付_0.37.0_rc1/补货助手';release=ROOT/'releases/0.37.0/rc1'
    build=read(delivery/'_internal/build_info.json');eq=read(qa/'equivalence.json');exe=sha(delivery/'补货助手.exe')
    if build['version']!='0.37.0' or not eq['passed'] or eq['difference_count']:raise ValueError('Build or strict comparison failed')
    for name in ['exe-scheduled/report.json','exe-indicators/report.json','exe-catalog/report.json']:
        r=read(qa/name)
        if not r['passed'] or not r['frozen'] or r['process_exit'] or r['version']['source_commit']!=build['source_commit'] or r['exe_sha256']!=exe:raise ValueError('EXE evidence mismatch '+name)
    ax=read(qa/'exe-aoxiang/smoke.json')
    if not ax['passed'] or not ax['actual_exe'] or ax['source_commit']!=build['source_commit']:raise ValueError('AX EXE mismatch')
    ui=read(qa/'exe-scheduled/report.json')
    if ui['ui_p95_gap_ms']>100 or ui['ui_max_gap_ms']>500:raise ValueError('UI responsiveness target missed; report before sealing')
    perf=read(qa/'performance-final/report.json')
    if not perf['passed']:raise ValueError('Performance target missed')
    built=ROOT/'build_0.37.0_rc1/source'
    if source_hashes(ROOT)!=source_hashes(built) or source_hashes(ROOT)!=read(qa/'after/capture.json')['source_hashes']:raise ValueError('Source changed after comparison or build')
    raw=(qa/'python-tests.txt').read_text('utf-8')
    if not re.search(r'Ran 791 tests',raw) or not raw.rstrip().endswith('OK'):raise ValueError('Full regression failed')
    for name,count in [('full-aoxiang2.txt',168),('full-jst.txt',41)]:
        raw=(qa/name).read_text('utf-8')
        if not re.search(r'pass '+str(count)+r'\b',raw) or not re.search(r'fail 0\b',raw):raise ValueError('Component tests failed '+name)
    baseline=read(qa/'baseline.json');documents={str(ROOT/n) for n in ['README.md','docs/项目意图与实施进度.md','docs/plans/业务使用体验优化与验收清单_20261104.md']}
    changed=[p for p,h in baseline['protected'].items() if p not in documents and (not Path(p).is_file() or sha(p)!=h)]
    if changed:raise ValueError('Protected data changed '+str(changed))
    release.mkdir(parents=True,exist_ok=False)
    copies={'equivalence.json':'equivalence.json','python-tests.txt':'python-tests.txt','full-aoxiang2.txt':'aoxiang-tests.txt','full-jst.txt':'jst-tests.txt',
        'scheduled-final.txt':'scheduler-tests.txt','source-ui/report.json':'source-ui.json','exe-scheduled/report.json':'exe-scheduled.json',
        'exe-indicators/report.json':'exe-indicators.json','exe-catalog/report.json':'exe-catalog.json','exe-aoxiang/smoke.json':'exe-aoxiang.json',
        'exe-aoxiang/shared-session.json':'exe-aoxiang-shared.json','performance-final/report.json':'performance.json'}
    for source,target in copies.items():(release/target).write_bytes((qa/source).read_bytes().replace(b'\r\n',b'\n'))
    for name in ['01-执行安排.png','02-运行记录.png','03-核对与签发.png']:(release/name).write_bytes((qa/'exe-scheduled'/name).read_bytes())
    live=read(qa/'live-qingdao/report.json')
    # Do not copy browser configuration, local binding paths or business inputs into Git.
    save(release/'live-status.json',{'plan':'青岛线','stores':6,'route':'山东仓单仓','budget':'不限预算','scope':'isolated QA only',
        'passed':live['passed'],'stage':'Edge connectOverCDP','observed':'Connection timed out after 180000 ms; no export run was created',
        'native_exports_created':False,'run_id_created':(qa/'live-qingdao/run-id.txt').exists()})
    save(release/'baseline.json',{'commit':baseline['baseline'],'source_zip_sha256':baseline['source_zip_sha256'],
        'protected_files':sum(p not in documents for p in baseline['protected']),'protected_changed':changed,'core':eq['core']})
    files={p.relative_to(delivery).as_posix():sha(p) for p in sorted(delivery.rglob('*')) if p.is_file()}
    save(release/'files.json',{'build':build,'files':files})
    archive=delivery.parent/'补货助手_0.37.0_rc1_定时补货.zip'
    with zipfile.ZipFile(archive,'x',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for name in files:z.write(delivery/name,'补货助手/'+name)
    with zipfile.ZipFile(archive) as z:
        if z.testzip() or len(z.namelist())!=len(files):raise ValueError('ZIP integrity failure')
        for name,digest in files.items():
            if hashlib.sha256(z.read('补货助手/'+name)).hexdigest()!=digest:raise ValueError('ZIP member mismatch')
    result={'version':'0.37.0','model_version':'0.3.4-rules','baseline':baseline['baseline'],'build_commit':build['source_commit'],
        'exe_sha256':exe,'zip_sha256':sha(archive),'archive':str(archive),'delivery':str(delivery),'files':len(files),
        'python_tests':791,'aoxiang_tests':168,'jst_tests':41,'business_differences':0,'cases_including_excel':len(eq['cases']),'protected_unchanged':True,
        'real_pipeline_passed':False,'overnight_tested':False,'idle_cpu_5min_tested':False,'pushed':False,'merged':False,'release_status':'local candidate; real platform integration blocked'}
    save(release/'package.json',result);print(json.dumps(result,ensure_ascii=False))
if __name__=='__main__':main()
