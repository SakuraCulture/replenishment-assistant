"""Seal 0.11.0 candidate only after independent reports and actual EXE acceptance."""
import hashlib,json,shutil,subprocess,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];Q=ROOT/'qa/archive_011';DIST=ROOT/'交付_0.11.0_rc2';APP=DIST/'补货助手';REL=ROOT/'releases/0.11.0/rc2'
sha=lambda p:hashlib.sha256(Path(p).read_bytes()).hexdigest()
load=lambda p:json.loads(Path(p).read_text(encoding='utf-8-sig'))


def main():
    info=load(APP/'_internal/build_info.json');head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    if info['source_commit']!=head:raise ValueError('Seal before acceptance-evidence commit; build source must still be HEAD')
    if subprocess.check_output(['git','diff','HEAD','--','src','prepare_delivery.py'],cwd=ROOT):raise ValueError('Runtime source changed after build')
    reports={'equivalence.json':Q/'equivalence.json','matrix.json':Q/'matrix_comparison.json','transactions.json':Q/'transactions/report.json','delivery.json':Q/'delivery_regression/report.json','real_archives.json':Q/'real_archives/report.json','exe.json':Q/'exe_rc2/report.json'}
    for name,p in reports.items():
        if not load(p)['passed']:raise ValueError('Validation failed: '+name)
    exe=load(reports['exe.json'])
    if any(r['development'] or r['exit_code'] or r['remaining_children'] or r['exe_sha256']!=sha(APP/'补货助手.exe') or r['version']['source_commit']!=head for r in exe['runs']):raise ValueError('EXE evidence does not bind build')
    protected=load(Q/'protected.json');changes=[p for p,h in protected.items() if not Path(p).is_file() or sha(p)!=h]
    if changes:raise ValueError('Protected original changed: '+repr(changes))
    core=['model.py','allocation.py','one_box_exception.py','default_config.json','default_must_sell.json','engine.py','input_adapter.py','batch_service.py','warehouse_catalog.py','new_products.py','result_semantics.py','parameter_schema.py','normal_exports.py','storage_chunks.py','work_repository.py']
    hashes={n:{'baseline':sha(Q/'baseline/src'/n),'candidate':sha(ROOT/'src'/n)} for n in core}
    if any(h['baseline']!=h['candidate'] for h in hashes.values()):raise ValueError('Business module changed')
    REL.mkdir(parents=True,exist_ok=False)
    for name,p in reports.items():shutil.copy2(p,REL/name)
    for name in ['baseline.json','units.txt','portable_tests.txt','portable_optimized.txt','py310_tests.txt','py310_optimized.txt','environment.txt']:shutil.copy2(Q/name,REL/name)
    for name in ['archive-warehouse-list.png','archive-calculation-form.png','archive-recycle-readonly.png','archive-restored.png']:shutil.copy2(Q/'exe_rc2'/name,REL/name)
    artifacts={str(p.relative_to(APP)):sha(p) for p in APP.rglob('*') if p.is_file()}
    (REL/'files.json').write_text(json.dumps(artifacts,ensure_ascii=False,indent=2),encoding='utf-8')
    archive=DIST/'补货助手_0.11.0_rc2.zip'
    with zipfile.ZipFile(archive,'x',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for rel in artifacts:z.write(APP/rel,'补货助手/'+rel)
    with zipfile.ZipFile(archive) as z:
        if z.testzip() is not None:raise ValueError('ZIP CRC failed')
        if any(hashlib.sha256(z.read('补货助手/'+rel)).hexdigest()!=h for rel,h in artifacts.items()):raise ValueError('ZIP content hash differs')
    result={'passed':True,'baseline_commit':'dd76b57cc00a038b0fb3d15973e68fd1c712c137','build_commit':head,'version':'0.11.0','model_version':'0.3.3-rules','exe_sha256':sha(APP/'补货助手.exe'),'zip_sha256':sha(archive),'file_count':len(artifacts),'protected_count':len(protected),'protected_changes':changes,'unchanged_business_files':hashes,'allowed_changes':['warehouse history projection and child identity','three-sheet archive export only','archive_visibility table and recycle/restore audit events','reject signing/merchant export/replies to recycled child until restore','Python type contracts and Qt-free test imports'],'business_snapshot_rewrite':False,'exe_tests':{'runs':len(exe['runs']),'synthetic_ui':True,'real_business_regression_separate':True}}
    (REL/'release_report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:result[k] for k in ('passed','build_commit','file_count','protected_count','exe_sha256','zip_sha256')}))


if __name__=='__main__':main()
