"""Seal independently verified 0.39.0 artifacts; never publishes or overwrites."""
import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import zipfile

ROOT=Path(__file__).resolve().parents[1]


def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    return h.hexdigest()


def read(path):return json.loads(Path(path).read_text('utf-8'))


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--qa',type=Path,required=True)
    parser.add_argument('--delivery',type=Path,required=True);parser.add_argument('--release',type=Path,required=True)
    parser.add_argument('--zip',type=Path,required=True);args=parser.parse_args()
    qa,delivery,release,archive=(p.resolve() for p in (args.qa,args.delivery,args.release,args.zip))
    if release.exists() or archive.exists():raise FileExistsError('Use new evidence and ZIP paths')
    build=read(delivery/'_internal/build_info.json');baseline=read(qa/'baseline.json')
    if build['version']!='0.39.0':raise ValueError('Wrong build version')
    native=read(qa/'exe-rc2/exe-report.json');native_proof=read(qa/'exe-rc2/delivery-evidence.json')
    ipc=read(qa/'stdio-exe-rc2/automation-report.json');ipc_proof=read(qa/'stdio-exe-rc2/delivery-evidence.json')
    result=read(qa/'benchmark-final/report.json');interactions=read(qa/'interaction-performance/report.json')
    if not all(r.get('passed') and r.get('frozen_exe') for r in (native,ipc)):raise ValueError('Actual EXE acceptance required')
    if native_proof['build']!=build or ipc_proof['build']!=build or not native_proof['exe_unchanged'] or not ipc_proof['unchanged']:
        raise ValueError('Acceptance belongs to another build or package changed')
    if result['equivalence']['baseline']!=baseline['commit'] or result['equivalence']['difference_count']!=0:raise ValueError('Baseline comparison failed')
    exe=delivery/'补货助手.exe';bridge=delivery/'AI接口/补货助手AI接口.exe'
    if sha(exe)!=native_proof['exe_sha256']:raise ValueError('EXE changed after verification')
    for path,digest in ipc_proof['hashes'].items():
        if sha(path)!=digest:raise ValueError('IPC acceptance artifact changed')
    checked=[]
    for path,digest in baseline['files'].items():
        frozen=subprocess.check_output(['git','show',build['source_commit']+':'+path],cwd=ROOT)
        actual=sha(ROOT/path)
        if actual!=digest or hashlib.sha256(frozen).hexdigest()!=digest:raise ValueError('Protected business file changed: '+path)
        checked.append({'path':path,'baseline_sha256':digest,'candidate_sha256':actual,'unchanged':True})
    if subprocess.check_output(['git','diff',build['source_commit'],'--','src','tests'],cwd=ROOT):
        raise ValueError('Runtime/test changes after build require re-verification')
    raw=(qa/'all-tests-final.log').read_bytes()
    log=raw.decode('utf-16') if raw.startswith(b'\xff\xfe') else raw.decode('utf-8')
    match=re.search(r'Ran (\d+) tests in ([\d.]+)s\s+OK\s*$',log)
    if not match:raise ValueError('Final complete unittest result missing')
    release.mkdir(parents=True);archive.parent.mkdir(parents=True,exist_ok=True)
    proofs={
        'baseline.json':'baseline.json','benchmark-final/report.json':'性能与严格对照.json',
        'interaction-performance/report.json':'交互分段性能.json','exe-rc2/exe-report.json':'EXE原生验收.json',
        'exe-rc2/delivery-evidence.json':'EXE构建依据.json','stdio-exe-rc2/automation-report.json':'EXE接口验收.json',
        'stdio-exe-rc2/delivery-evidence.json':'接口构建依据.json',
        'exe-rc2/经营分析.png':'经营分析.png','exe-rc2/商品漏斗.png':'商品漏斗.png','exe-rc2/安全库存诊断.png':'安全库存诊断.png'}
    for source,name in proofs.items():shutil.copy2(qa/source,release/name)
    # Preserve original line endings while transcoding text; Windows write_text
    # would turn existing CRLF into CRCRLF.
    (release/'全部回归.log.txt').write_bytes(log.encode('utf-8'))
    manifest=[]
    for path in sorted(delivery.rglob('*')):
        if path.is_file():manifest.append({'path':path.relative_to(delivery).as_posix(),'bytes':path.stat().st_size,'sha256':sha(path)})
    with zipfile.ZipFile(archive,'x',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for item in manifest:z.write(delivery/item['path'],delivery.name+'/'+item['path'])
    with zipfile.ZipFile(archive) as z:
        if z.testzip() is not None:raise ValueError('ZIP integrity check failed')
        for item in manifest:
            if hashlib.sha256(z.read(delivery.name+'/'+item['path'])).hexdigest()!=item['sha256']:raise ValueError('ZIP contents differ')
    (release/'交付文件清单.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),'utf-8')
    summary={'version':'0.39.0','build':build,'branch':subprocess.check_output(['git','branch','--show-current'],cwd=ROOT,text=True).strip(),
             'baseline_commit':baseline['commit'],'protected_files':checked,
             'unit_tests':{'count':int(match[1]),'seconds':float(match[2]),'passed':True,
                'warning':'完整套件Qt控件清理期间有5条QLabel已销毁回调告警；已保留全日志。本轮实际EXE两个独立验收进程未出现该告警，不宣称本轮修复了这项遗留。'},
             'native_exe_passed':True,'stdio_exe_passed':True,'strict_difference_count':0,
             'exe_sha256':sha(exe),'bridge_sha256':sha(bridge),'zip':str(archive),'zip_sha256':sha(archive),
             'zip_bytes':archive.stat().st_size,'delivery_files':len(manifest),'delivery_bytes':sum(i['bytes'] for i in manifest),
             'environment':{'python':sys.version,'platform':platform.platform(),'packages':{x:importlib.metadata.version(x) for x in ['PySide6-Essentials','openpyxl','PyInstaller']}},
             'not_verified':['真实全量历史数据质量','外部AI客户端自然语言协作','新机器运行','冷盘缓存和长期规模','平台实际取数'],
             'unchanged_boundary':'原模型、默认配置、旧四段服务及签发导出核心文件与基线SHA一致；所有验收使用隔离数据。没有写入日常配置/数据库、修改历史或覆盖旧包；未推送、未合并。'}
    (release/'交付核验.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),'utf-8')
    print(json.dumps({'release':str(release),'zip':str(archive),'zip_sha256':summary['zip_sha256'],'exe_sha256':summary['exe_sha256'],'file_count':len(manifest)},ensure_ascii=False,indent=2))


if __name__=='__main__':main()
