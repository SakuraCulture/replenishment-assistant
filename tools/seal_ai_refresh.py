"""Seal 0.38.0 local evidence after actual package acceptance; never publishes."""
import argparse
import hashlib
import json
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    with Path(path).open('rb') as f: return hashlib.file_digest(f, 'sha256').hexdigest()


def read(path): return json.loads(Path(path).read_text('utf-8'))


def main():
    p=argparse.ArgumentParser(); p.add_argument('--qa',type=Path,required=True);p.add_argument('--delivery',type=Path,required=True);p.add_argument('--release',type=Path,required=True)
    p.add_argument('--expected-version',default='0.38.0');p.add_argument('--minimum-tests',type=int,default=803)
    args=p.parse_args();q=args.qa.resolve();d=args.delivery.resolve();dest=args.release.resolve()
    info=read(d/'_internal/build_info.json');version=read(d/'_internal/version.json')
    if version['version']!=args.expected_version:raise ValueError('Wrong delivery version')
    evidence={}
    for name in ('equivalence.json','source-final/automation-report.json','source-final/refresh-report.json','exe/automation-report.json','exe/refresh-report.json','exe/playbook-report.json'):
        value=read(q/name)
        if not value['passed']:raise ValueError('Acceptance failed: '+name)
        evidence[name]=value
    packaged=read(q/'exe/delivery-evidence.json')
    if not evidence['exe/automation-report.json']['frozen_exe'] or not packaged['unchanged'] or packaged['build']!=info:raise ValueError('Package evidence mismatch')
    for file,h in packaged['hashes'].items():
        if sha(file)!=h:raise ValueError('Executable changed after acceptance')
    raw=(q/'unittest-final.log').read_text('utf-8-sig');counts=re.findall(r'Ran (\d+) tests',raw)
    if not counts or int(counts[-1])<args.minimum_tests or not re.search(r'\nOK\s*$',raw):raise ValueError('Full regression did not pass')
    baseline=read(q/'baseline.json')
    approved_docs={str(ROOT/n) for n in ('README.md','docs/项目意图与实施进度.md','docs/plans/业务使用体验优化与验收清单_20261104.md')}
    changed=[f for f,h in baseline['protected'].items() if f not in approved_docs and (not Path(f).is_file() or sha(f)!=h)]
    if changed:raise ValueError('Protected data or old package changed')
    if info['source_commit']!=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip():raise ValueError('Seal from the build commit before documentation commit')
    if subprocess.check_output(['git','status','--porcelain'],cwd=ROOT,text=True).strip():raise ValueError('Working tree must be clean before sealing')
    archive=d.parent/('补货助手_'+version['version']+'_'+info['build_id']+'.zip')
    if archive.exists() or dest.exists():raise FileExistsError('Evidence or archive already exists; do not overwrite')
    manifest=[{'path':str(f.relative_to(d)).replace('\\','/'),'bytes':f.stat().st_size,'sha256':sha(f)} for f in sorted(d.rglob('*')) if f.is_file()]
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
        for entry in manifest:z.write(d/entry['path'],'补货助手/'+entry['path'])
    with zipfile.ZipFile(archive) as z:
        if z.testzip():raise ValueError('ZIP integrity failed')
        for e in manifest:
            if hashlib.sha256(z.read('补货助手/'+e['path'])).hexdigest()!=e['sha256']:raise ValueError('ZIP member mismatch')
    dest.mkdir(parents=True)
    for name,value in evidence.items():
        # Full transcripts may contain synthetic data; only summary receipts are tracked.
        if name.endswith('playbook-report.json'):value={k:v for k,v in value.items() if k!='transcript'}
        target=dest/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_text(json.dumps(value,ensure_ascii=False,indent=2),'utf-8')
    for name in ('unittest-final.log','automation-final.log'):shutil.copy2(q/name,dest/(name+'.txt'))
    # Preserve failed discovery evidence separately, never count it as acceptance.
    for name in ('automation-report.json','settings/app.log'):
        original=q/'source-initial-failed'/name
        if original.exists():
            target=dest/'initial-failure'/(name+'.txt');target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(original,target)
    result={'baseline':baseline['baseline'],'build':info,'version':version,'full_tests':int(counts[-1]),
            'business_differences':evidence['equivalence.json']['difference_count'],
            'protected_file_count':len(baseline['protected'])-len(approved_docs),'protected_changes':changed,
            'delivery':str(d),'exe_sha256':sha(d/'补货助手.exe'),'bridge_sha256':sha(d/'AI接口/补货助手AI接口.exe'),
            'zip':str(archive),'zip_sha256':sha(archive),'files':manifest,
            'limits':['No live platform exports or scheduled writes','No production client configuration changes','No natural-language model success rate claim','No fresh-PC or byte-reproducible build claim']}
    (dest/'release.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),'utf-8')
    print(json.dumps({k:v for k,v in result.items() if k!='files'},ensure_ascii=False,indent=2))


if __name__=='__main__':main()
