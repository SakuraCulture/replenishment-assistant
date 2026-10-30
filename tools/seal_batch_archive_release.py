"""Seal local 0.12.0 candidate only after fresh regression and actual EXE checks."""
import hashlib,json,shutil,subprocess,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];Q=ROOT/'qa/batch_archive_012';DIST=ROOT/'交付_0.12.0_rc2';APP=DIST/'补货助手';REL=ROOT/'releases/0.12.0/rc2'
def sha(p):
    h=hashlib.sha256()
    with Path(p).open('rb') as f:
        for block in iter(lambda:f.read(4*1024*1024),b''):h.update(block)
    return h.hexdigest()
def load(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def save(p,value):Path(p).write_bytes((json.dumps(value,ensure_ascii=False,indent=2)+'\n').encode())
def main():
    head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip();build=load(APP/'_internal/build_info.json')
    if build['source_commit']!=head:raise ValueError('Seal at exact build commit before evidence commit')
    if subprocess.check_output(['git','diff','HEAD','--','src','prepare_delivery.py'],cwd=ROOT):raise ValueError('Runtime changed after build')
    reports={'equivalence.json':Q/'equivalence.json','real_exports.json':Q/'real-exports/report.json','xml_equivalence.json':Q/'real-exports/xml_equivalence.json','exe.json':Q/'exe-rc2/report.json'}
    for name,p in reports.items():
        if not load(p)['passed']:raise ValueError('Failed validation '+name)
    exe=load(reports['exe.json'])
    if any(r['development'] or r['exit_code'] or r['remaining_children'] or r['exe_sha256']!=sha(APP/'补货助手.exe') or r['version']['source_commit']!=head for r in exe['runs']):raise ValueError('EXE evidence does not bind build')
    for name in ['units.txt','optimized-tests.txt','portable310.txt']:
        if not (Q/name).read_text(encoding='utf-8',errors='replace').rstrip().endswith('OK'):raise ValueError('Tests not passed '+name)
    protected=load(Q/'protected.json');changed=[p for p,h in protected.items() if not Path(p).is_file() or sha(p)!=h]
    if changed:raise ValueError('Protected file changed: '+repr(changed))
    core=['model.py','allocation.py','one_box_exception.py','default_config.json','default_must_sell.json','engine.py','input_adapter.py','batch_service.py','warehouse_catalog.py','new_products.py','result_semantics.py','parameter_schema.py','normal_exports.py','storage_chunks.py','work_repository.py']
    hashes={n:{'baseline':sha(Q/'baseline/src'/n),'candidate':sha(ROOT/'src'/n)} for n in core}
    if any(v['baseline']!=v['candidate'] for v in hashes.values()):raise ValueError('Core business source changed')
    if (REL/'release_report.json').exists():raise ValueError('Already sealed')
    REL.mkdir(parents=True,exist_ok=True)
    for name,p in reports.items():shutil.copy2(p,REL/name)
    for name in ['baseline.json','units.txt','optimized-tests.txt','portable310.txt','doctor.txt']:shutil.copy2(Q/name,REL/name)
    for name in ['batch-archive-toolbar.png','batch-preview-calculation.png','batch-preview-signed.png','batch-preview-original.png','batch-result.png','archive-restored.png']:shutil.copy2(Q/'exe-rc2'/name,REL/name)
    files={p.relative_to(APP).as_posix():sha(p) for p in APP.rglob('*') if p.is_file()};save(REL/'files.json',files)
    archive=DIST/'补货助手_0.12.0_rc2.zip'
    if archive.exists():raise ValueError('ZIP already exists')
    with zipfile.ZipFile(archive,'x',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for name in files:z.write(APP/name,'补货助手/'+name)
    with zipfile.ZipFile(archive) as z:
        if z.testzip() or set(z.namelist())!={'补货助手/'+n for n in files}:raise ValueError('ZIP file set/CRC failed')
        if any(hashlib.sha256(z.read('补货助手/'+n)).hexdigest()!=h for n,h in files.items()):raise ValueError('ZIP content differs')
    result={'passed':True,'baseline_commit':load(Q/'baseline.json')['commit'],'build_commit':head,'version':'0.12.0','model_version':'0.3.3-rules','exe_sha256':sha(APP/'补货助手.exe'),'zip_sha256':sha(archive),'file_count':len(files),'protected_count':len(protected),'protected_changes':changed,'unchanged_business_files':hashes,'no_remote_actions':True,'source_db_changes':False,'allowed_changes':['three independent archive export entrypoints','batch selection and warehouse-separated file organization','six identity columns; union absent fields blank; autofilter/freeze panes','original Excel byte copies instead of misleading form exports','optional labelled derived master-data reference table','derived index schema3; UI-only export preference file','recycled orders blocked from legacy download entries'],'limitations':['missing historical originals cannot be reconstructed','single Excel save/decode/read waits for safe cancellation boundary','new derived index initial cold build','no full clean-machine reproducible build claim','real-workbook timings are single-run QA, not measured speedup']}
    save(REL/'release_report.json',result)
    # Normalize evidence text only; keep all original runtime/package bytes unchanged.
    for p in REL.iterdir():
        if p.suffix in ('.json','.txt','.md'):p.write_bytes(p.read_text(encoding='utf-8-sig',errors='replace').encode('utf-8'))
    print(json.dumps({k:result[k] for k in ('passed','build_commit','file_count','protected_count','exe_sha256','zip_sha256')}))

if __name__=='__main__':main()
