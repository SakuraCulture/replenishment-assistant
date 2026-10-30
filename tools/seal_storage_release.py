"""Seal a clean, verified 0.8.0 candidate without replacing existing releases."""
import argparse,hashlib,json,shutil,subprocess,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def load(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def sha(p):
    with Path(p).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def main():
    p=argparse.ArgumentParser();p.add_argument('--delivery',type=Path,required=True);p.add_argument('--qa',type=Path,required=True);a=p.parse_args();dest=a.delivery.resolve();qa=a.qa.resolve();meta=load(dest/'版本信息.json')
    assert meta['version']=='0.8.0';head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    assert not subprocess.check_output(['git','status','--porcelain'],cwd=ROOT,text=True).strip()
    subprocess.run(['git','merge-base','--is-ancestor',meta['source_commit'],head],cwd=ROOT,check=True)
    changes=[n for n in subprocess.check_output(['git','diff','--name-only','-z',meta['source_commit'],head],cwd=ROOT).decode('utf-8').split('\0') if n]
    assert all(x.startswith(('docs/','tools/','releases/')) or x in ('README.md','使用说明.txt') for x in changes)
    files={'equivalence':qa/'equivalence_release/report.json','controls':qa/'controls_release/report.json','selected_controls':qa/'selected_controls_release/report.json','normal':qa/'normal_final/normal_report.json','performance':qa/'performance_release/report.json','workbooks':qa/'workbooks.json','preservation':qa/'protected_after.json'}
    files.update(source_audit=qa/'source_audit.json',chunks=qa/'chunks_fullparse/report.json',projections=qa/'projections_final/report.json',maintenance=qa/'maintenance_final/report.json',rule_fix=qa/'stockout_fix.json',real_recalculation=qa/'real_current/report.json',real_conversion=qa/'real_conversion/report.json')
    ui={'storage_ui':qa/'exe-storage/storage_ui_report.json','normal_ui':qa/'exe-normal/normal_ui_report.json','delivery_ui':qa/'exe-delivery/export_ui_report.json','history_ui':qa/'exe-history/history_ui_report.json','work_io_ui':qa/'exe-work-io/work_io_report.json'}
    evidence={}
    for name,path in {**files,**ui}.items():
        data=load(path);assert data['passed'],name;evidence[name]=data
        if name in ui:
            assert data['exe_sha256']==sha(dest/'补货助手.exe'),name
            version=data['version'];build=version.get('source_commit') if isinstance(version,dict) else data.get('source_commit')
            assert build==meta['source_commit'],(name,build,meta['source_commit'])
    assert evidence['performance']['runs']==3
    progress=evidence['work_io_ui']['progress']
    assert all(progress.get(name) for name in ('save','confirm_normal','merchant_export'))
    assert any(row['maximum']==1000 for row in progress['save'])
    assert any(row['maximum']==0 for row in progress['save'])
    assert all('已用时' in row['health'] for rows in progress.values() for row in rows)
    merchant=list((qa/'exe-work-io/merchant').glob('*.xlsx'));assert len(merchant)==5
    assert all(f.stem.endswith('（精简版）') for f in merchant)
    actual={f.name:sha(f) for f in (ROOT/'src').glob('*') if f.is_file()}
    for kind in ('old','new'):
        for i in range(3):
            run=load(qa/f'performance_release/{kind}-{i}/result.json');assert run['passed'] and not run['remaining_children']
            if kind=='new':assert run['source_hashes']==actual,'source changed after performance measurement'
    out=ROOT/'releases/0.8.0'/meta['build_id'];out.mkdir(parents=True,exist_ok=False)
    for name,path in {**files,**ui}.items():
        (out/(name+'.json')).write_text(path.read_text(encoding='utf-8-sig'),encoding='utf-8',newline='\n')
    shutil.copy2(qa/'交付报告.md',out/'交付报告.md');shutil.copy2(qa/'交付报告.md',dest/'性能与验收报告.md')
    receipt={'passed':True,'version':'0.8.0','baseline_commit':'39f49442c66bec6885873f75858514406a43cf45','source_commit':meta['source_commit'],'evidence_commit':head,'build_id':meta['build_id'],'exe_sha256':sha(dest/'补货助手.exe'),'storage_format_version':2,'storage_business_differences':0,'model_rule_version':'0.3.1-rules','authorized_rule_change':'only N>30 and J>0 discards contradictory stockout days; original N and sales formula retained','old_package_and_inputs_preserved':True,'delivery':str(dest),'no_remote_push_or_merge':True,'limits':['full restore still reads/transfers full snapshot; encoding traverses all values','projection verifies only accessed blocks; full reads and copy conversion verify full values','old EXE requires a legacy copy; old/new model jobs readonly across model versions','no chunk GC or automatic all-history conversion','timings specific to five-store fixture and this Windows computer']}
    (dest/'验收记录.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8',newline='\n')
    manifest={f.relative_to(dest).as_posix():sha(f) for f in dest.rglob('*') if f.is_file()};(out/'files_sha256.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8',newline='\n')
    archive=dest.parent/f"补货助手_0.8.0_{meta['build_id']}.zip"
    with zipfile.ZipFile(archive,'x',zipfile.ZIP_DEFLATED) as z:
        for rel in manifest:z.write(dest/rel,dest.name+'/'+rel)
    with zipfile.ZipFile(archive) as z:assert z.testzip() is None
    receipt.update(zip=str(archive),zip_sha256=sha(archive));(out/'release_report.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8',newline='\n');print(json.dumps(receipt,ensure_ascii=False))
if __name__=='__main__':main()
