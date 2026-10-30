"""Seal the isolated 0.13.0 candidate after actual EXE and ordered equivalence."""
import hashlib,json,shutil,subprocess,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];Q=ROOT/'qa/interaction_013';DIST=ROOT/'交付_0.13.0_rc1';APP=DIST/'补货助手';REL=ROOT/'releases/0.13.0/rc1'

def sha(p):
    h=hashlib.sha256()
    with Path(p).open('rb') as f:
        for block in iter(lambda:f.read(4*1024*1024),b''):h.update(block)
    return h.hexdigest()

def load(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def save(p,v):Path(p).write_bytes((json.dumps(v,ensure_ascii=False,indent=2)+'\n').encode())

def main():
    head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip();build=load(APP/'_internal/build_info.json')
    if build['source_commit']!=head:raise ValueError('Seal at exact build commit')
    if subprocess.check_output(['git','diff','HEAD','--','src','prepare_delivery.py'],cwd=ROOT):raise ValueError('Runtime changed since build')
    reports={'equivalence.json':Q/'equivalence.json','archive_exports.json':Q/'archive-exports-2/report.json','source_ui.json':Q/'source-final/report.json','exe.json':Q/'exe-rc1/report.json','boundaries.json':Q/'boundaries/report.json'}
    for name,p in reports.items():
        if not load(p)['passed']:raise ValueError('Validation failed '+name)
    exe=load(reports['exe.json'])
    if any(r['development'] or r['exit_code'] or r['remaining_children'] or r['exe_sha256']!=sha(APP/'补货助手.exe') or r['version']['source_commit']!=head for r in exe['runs']):raise ValueError('EXE evidence mismatch')
    for name in ('units.txt','optimized-tests.txt'):
        if not (Q/name).read_text(encoding='utf-8',errors='replace').rstrip().endswith('OK'):raise ValueError('Unit tests failed '+name)
    protected={**load(Q/'protected.json'),**load(Q/'inputs.json')};changed=[p for p,h in protected.items() if not Path(p).is_file() or sha(p)!=h]
    if changed:raise ValueError('Protected files changed: '+repr(changed))
    core=['model.py','allocation.py','one_box_exception.py','default_config.json','default_must_sell.json','engine.py','input_adapter.py','batch_service.py','warehouse_catalog.py','new_products.py','result_semantics.py','parameter_schema.py','normal_exports.py','archive_batch.py','archive_exports.py','xlsx_exports.py','merchant_feedback.py','storage_chunks.py','work_repository.py','normal_orders.py','normal_delivery.py','work_io_process.py','history_forms.py']
    hashes={n:{'baseline':sha(Q/'baseline/src'/n),'candidate':sha(ROOT/'src'/n)} for n in core}
    if any(v['baseline']!=v['candidate'] for v in hashes.values()):raise ValueError('Business implementation changed')
    if (REL/'release_report.json').exists():raise ValueError('Already sealed')
    REL.mkdir(parents=True,exist_ok=True)
    for name,p in reports.items():shutil.copy2(p,REL/name)
    for name in ('baseline.json','inputs.json','units.txt','optimized-tests.txt','doctor.txt'):shutil.copy2(Q/name,REL/name)
    for p in (Q/'exe-rc1').glob('interaction-*.png'):shutil.copy2(p,REL/p.name)
    for name in ('batch-archive-toolbar.png','archive-restored.png','sign-preview.png'):shutil.copy2(Q/'exe-rc1'/name,REL/name)
    files={p.relative_to(APP).as_posix():sha(p) for p in APP.rglob('*') if p.is_file()};save(REL/'files.json',files)
    target=DIST/'补货助手_0.13.0_rc1.zip'
    with zipfile.ZipFile(target,'x',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for name in files:z.write(APP/name,'补货助手/'+name)
    with zipfile.ZipFile(target) as z:
        if z.testzip() or set(z.namelist())!={'补货助手/'+n for n in files}:raise ValueError('ZIP file set/CRC mismatch')
        if any(hashlib.sha256(z.read('补货助手/'+n)).hexdigest()!=h for n,h in files.items()):raise ValueError('ZIP content mismatch')
    result={'passed':True,'baseline_commit':load(Q/'baseline.json')['commit'],'build_commit':head,'version':'0.13.0','model_version':'0.3.3-rules','storage_format_version':2,'exe_sha256':sha(APP/'补货助手.exe'),'zip_sha256':sha(target),'file_count':len(files),'protected_count':len(protected),'protected_changes':changed,'unchanged_business_files':hashes,'allowed_changes':['UI labels, explicit selection scopes, confirmations and cancel-stage labels','atomic user-selected warehouse recycle/restore in existing visibility/event tables','current-order reply entry restricts order/warehouse/revision; global entry unchanged','preset management separated from store editor; explicit import replacement confirmation','new-product selection undo stays in uncommitted editor','application version/build metadata only'],'known_limits':['preset retirement has no restore UI','missing historical originals cannot be reconstructed','no performance speedup claim','Qt EXE automation is on this Windows computer, not all DPI/OS configurations','historical fixtures/real input QA remain local dependencies'],'no_remote_actions':True,'business_field_changes':[]}
    save(REL/'release_report.json',result)
    for p in REL.iterdir():
        if p.suffix in ('.json','.txt','.md'):p.write_bytes(p.read_text(encoding='utf-8-sig',errors='replace').encode())
    print(json.dumps({k:result[k] for k in ('passed','build_commit','file_count','protected_count','exe_sha256','zip_sha256')}))

if __name__=='__main__':main()
