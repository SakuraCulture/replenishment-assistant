"""Seal a clean, verified 0.7.4 candidate without replacing existing releases."""
import argparse,hashlib,json,shutil,subprocess,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def load(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def sha(p):
    with Path(p).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def main():
    p=argparse.ArgumentParser();p.add_argument('--delivery',type=Path,required=True);p.add_argument('--qa',type=Path,required=True);a=p.parse_args();dest=a.delivery.resolve();qa=a.qa.resolve();meta=load(dest/'版本信息.json')
    assert meta['version']=='0.7.4';head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    assert not subprocess.check_output(['git','status','--porcelain'],cwd=ROOT,text=True).strip()
    subprocess.run(['git','merge-base','--is-ancestor',meta['source_commit'],head],cwd=ROOT,check=True)
    changes=subprocess.check_output(['git','diff','--name-only',meta['source_commit'],head],cwd=ROOT,text=True).splitlines()
    assert all(x.startswith(('docs/','tools/','releases/')) or x in ('README.md','使用说明.txt') for x in changes)
    files={'equivalence':qa/'equivalence_final/report.json','controls':qa/'controls_final/report.json','selected_controls':qa/'selected_controls_final/report.json','normal':qa/'normal_final/normal_report.json','performance':qa/'performance_final/report.json','workbooks':qa/'workbooks.json','preservation':qa/'protected_after.json'}
    ui={'normal_ui':qa/'exe-normal/normal_ui_report.json','delivery_ui':qa/'exe-delivery/export_ui_report.json','history_ui':qa/'exe-history/history_ui_report.json','work_io_ui':qa/'exe-work-io/work_io_report.json'}
    evidence={}
    for name,path in {**files,**ui}.items():
        data=load(path);assert data['passed'],name;evidence[name]=data
        if name in ui:
            assert data['exe_sha256']==sha(dest/'补货助手.exe'),name
            version=data['version'];build=version.get('source_commit') if isinstance(version,dict) else data.get('source_commit')
            assert build==meta['source_commit'],(name,build,meta['source_commit'])
    assert evidence['performance']['runs']==3
    actual={f.name:sha(f) for f in (ROOT/'src').glob('*') if f.is_file()}
    for kind in ('old','new'):
        for i in range(3):
            run=load(qa/f'performance_final/{kind}-{i}/result.json');assert run['passed'] and not run['remaining_children']
            if kind=='new':assert run['source_hashes']==actual,'source changed after performance measurement'
    out=ROOT/'releases/0.7.4'/meta['build_id'];out.mkdir(parents=True,exist_ok=False)
    for name,path in {**files,**ui}.items():shutil.copy2(path,out/(name+'.json'))
    shutil.copy2(qa/'交付报告.md',out/'交付报告.md');shutil.copy2(qa/'交付报告.md',dest/'性能与验收报告.md')
    receipt={'passed':True,'version':'0.7.4','baseline_commit':'d3d0165cbc872e952b759bc6f2dbdff05437aff5','source_commit':meta['source_commit'],'evidence_commit':head,'build_id':meta['build_id'],'exe_sha256':sha(dest/'补货助手.exe'),'storage_format_unchanged':True,'business_differences':0,'old_package_and_inputs_preserved':True,'delivery':str(dest),'no_remote_push_or_merge':True,'limits':['full JSON snapshot encoding/transfer remains; no storage migration','first-stage progress is measured where countable, otherwise indeterminate with elapsed and last progress','no universal instant completion or cross-machine performance claim']}
    (dest/'验收记录.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8')
    manifest={f.relative_to(dest).as_posix():sha(f) for f in dest.rglob('*') if f.is_file()};(out/'files_sha256.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    archive=dest.parent/f"补货助手_0.7.4_{meta['build_id']}.zip"
    with zipfile.ZipFile(archive,'x',zipfile.ZIP_DEFLATED) as z:
        for rel in manifest:z.write(dest/rel,dest.name+'/'+rel)
    with zipfile.ZipFile(archive) as z:assert z.testzip() is None
    receipt.update(zip=str(archive),zip_sha256=sha(archive));(out/'release_report.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(receipt,ensure_ascii=False))
if __name__=='__main__':main()
