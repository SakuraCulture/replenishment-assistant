"""Seal 0.32.0 local candidate evidence after real frozen-EXE acceptance."""
import argparse,hashlib,json,subprocess,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];QA=ROOT/'qa/governance_032';DELIVERY=ROOT/'交付_0.32.0_rc1';APP=DELIVERY/'补货助手';RELEASE=ROOT/'releases/0.32.0/rc1'
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def read(p):return json.loads(p.read_text('utf-8'))
def write(p,v):p.write_text(json.dumps(v,ensure_ascii=False,indent=2),'utf-8',newline='\n')
def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verify-existing-archive',action='store_true',help='Resume sealing an existing ZIP only after verifying its complete contents; never rewrite it.')
    args=parser.parse_args()
    baseline=read(QA/'baseline.json');equivalence=read(QA/'final-equivalence.json');perf=read(QA/'delivery-final-r3/report.json');exe=read(QA/'exe-rc1/exe-report.json');build=read(ROOT/'build_0.32.0_rc1/build_info.json')
    if not all([equivalence['passed'],perf['passed'],exe['passed'],exe['frozen_exe']]):raise ValueError('Acceptance incomplete')
    if exe['source_commit']!=build['source_commit'] or exe['version']!='0.32.0':raise ValueError('Wrong executable/build')
    if 'Ran 638 tests' not in (QA/'final-all-tests.txt').read_text('utf-16' if (QA/'final-all-tests.txt').read_bytes().startswith(b'\xff\xfe') else 'utf-8',errors='replace'):raise ValueError('Unexpected regression report')
    subprocess.run(['git','diff','--exit-code',build['source_commit'],'--','src','prepare_delivery.py','tools/build_from_commit.py','components'],cwd=ROOT,check=True)
    changed=[p for p,h in baseline['protected'].items() if not Path(p).is_file() or sha(Path(p))!=h]
    if changed:raise ValueError('Protected files changed: '+repr(changed))
    cores={n:{'baseline':sha(QA/'baseline/src'/n),'candidate':sha(ROOT/'src'/n)} for n in ['model.py','one_box_exception.py','allocation.py','default_config.json']}
    if any(v['baseline']!=v['candidate'] for v in cores.values()):raise ValueError('Business core changed')
    if args.verify_existing_archive and RELEASE.is_dir():
        if any(RELEASE.iterdir()):raise ValueError('Release evidence already exists; do not overwrite')
    else:RELEASE.mkdir(parents=True,exist_ok=False)
    files={p.relative_to(APP).as_posix():sha(p) for p in APP.rglob('*') if p.is_file()}
    archive=DELIVERY/'补货助手_0.32.0_rc1_参数准入与补货计划.zip'
    if args.verify_existing_archive:
        if not archive.is_file():raise ValueError('Existing archive not found')
    else:
        with zipfile.ZipFile(archive,'x',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
            for relative in files:z.write(APP/relative,'补货助手/'+relative)
    with zipfile.ZipFile(archive) as z:
        expected={'补货助手/'+relative for relative in files}
        if len(z.namelist())!=len(expected) or set(z.namelist())!=expected:raise ValueError('ZIP file set mismatch')
        for relative,h in files.items():
            if hashlib.sha256(z.read('补货助手/'+relative)).hexdigest()!=h:raise ValueError('ZIP entry mismatch: '+relative)
    for source,name in [(QA/'final-equivalence.json','equivalence.json'),(QA/'final-all-tests.txt','regression.txt'),(QA/'delivery-final-r3/report.json','performance.json'),(QA/'exe-rc1/exe-report.json','exe-report.json')]:
        (RELEASE/name).write_bytes(source.read_bytes().replace(b'\r\n',b'\n'))
    write(RELEASE/'delivery-manifest.json',{'version':'0.32.0','build_commit':build['source_commit'],'source_archive_sha256':build['source_archive_sha256'],'exe_sha256':sha(APP/'补货助手.exe'),'zip':str(archive),'zip_sha256':sha(archive),'files':files})
    proof={str(p.relative_to(ROOT)):sha(p) for p in [QA/'baseline.json',QA/'baseline.zip',QA/'final-authorized-additions.jsonl',QA/'final/capture.json',QA/'delivery-final-r3/report.json',QA/'exe-rc1/exe-report.json']}
    write(RELEASE/'evidence.json',{'baseline_commit':baseline['commit'],'build_commit':build['source_commit'],'core':cores,'protected_file_count':len(baseline['protected']),'protected_changed':changed,'local_evidence':proof,'scope':'Only local QA, no user settings/input/old delivery writes, no remote publish or live procurement. Screenshot/UI setup synthetic; independent human usability walkthrough not performed.'})
    print(json.dumps({'release':str(RELEASE),'exe_sha256':sha(APP/'补货助手.exe'),'zip_sha256':sha(archive),'files':len(files),'protected_files':len(baseline['protected'])},ensure_ascii=False))
if __name__=='__main__':main()
