"""Seal only the exact tested 0.15.0 candidate; no merge/push/old-package writes."""
import hashlib,json,shutil,subprocess,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];Q=ROOT/'qa/lifecycle_015';DIST=ROOT/'交付_0.15.0_rc1';APP=DIST/'补货助手';REL=ROOT/'releases/0.15.0/rc1'

def sha(p):
    h=hashlib.sha256()
    with Path(p).open('rb') as f:
        for chunk in iter(lambda:f.read(4*1024*1024),b''):h.update(chunk)
    return h.hexdigest()
def load(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def save(p,v):Path(p).write_bytes((json.dumps(v,ensure_ascii=False,indent=2)+'\n').encode())

def main():
    head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip();build=load(APP/'_internal/build_info.json')
    if head!=build['source_commit'] or build['version']!='0.15.0':raise ValueError('Seal at exact candidate build commit')
    if subprocess.check_output(['git','status','--porcelain'],cwd=ROOT):raise ValueError('Build source checkout must still be clean')
    reports={'business-equivalence.json':Q/'business-equivalence.json','matrix.json':Q/'matrix.json','exports.json':Q/'exports/report.json','source-ui.json':Q/'source-ui-final/report.json','exe-lifecycle.json':Q/'exe-lifecycle/report.json','exe-existing.json':Q/'exe-existing/report.json','performance.json':Q/'performance/report.json'}
    for name,path in reports.items():
        if not load(path)['passed']:raise ValueError('Evidence failed: '+name)
    for name in ('exe-lifecycle.json','exe-existing.json'):
        if any(r['development'] or r['exit_code'] or r['remaining_children'] or r['exe_sha256']!=sha(APP/'补货助手.exe') or r['version']['source_commit']!=head for r in load(reports[name])['runs']):raise ValueError('Actual EXE/source identity mismatch')
    for name in ('units.txt','optimized-tests.txt'):
        if not (Q/name).read_text(encoding='utf-8-sig',errors='replace').rstrip().endswith('OK'):raise ValueError('Tests did not finish OK')
    protected=load(Q/'protected.json');changes=[p for p,h in protected.items() if not Path(p).is_file() or sha(p)!=h]
    if changes:save(Q/'protected-changes.json',changes);raise ValueError('Protected paths changed; see QA report')
    core=['model.py','allocation.py','one_box_exception.py','default_config.json','default_must_sell.json','engine.py','input_adapter.py','batch_service.py','warehouse_catalog.py','new_products.py','result_semantics.py','parameter_schema.py','normal_exports.py','archive_batch.py','archive_exports.py','xlsx_exports.py','merchant_feedback.py','storage_chunks.py','work_repository.py','normal_orders.py','normal_delivery.py','work_io_process.py','history_forms.py']
    hashes={n:{'baseline':sha(Q/'baseline/src'/n),'candidate':sha(ROOT/'src'/n)} for n in core}
    if any(h['baseline']!=h['candidate'] for h in hashes.values()):raise ValueError('Protected business implementation differs')
    if sha(ROOT/'requirements-win-amd64.lock')!=sha(Q/'baseline/requirements-win-amd64.lock'):raise ValueError('Deferred dependency changes included')
    REL.mkdir(parents=True,exist_ok=False)
    for name,path in reports.items():shutil.copy2(path,REL/name)
    for name in ('baseline.json','units.txt','optimized-tests.txt','environment.json'):shutil.copy2(Q/name,REL/name)
    for name in ('home-complete.png','lifecycle-comparison.png','document-versions.png','preview-payment-save.png'):shutil.copy2(Q/'exe-lifecycle'/name,REL/name)
    files={p.relative_to(APP).as_posix():sha(p) for p in APP.rglob('*') if p.is_file()};save(REL/'files.json',files);target=DIST/'补货助手_0.15.0_rc1.zip'
    with zipfile.ZipFile(target,'x',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for name in files:z.write(APP/name,'补货助手/'+name)
    with zipfile.ZipFile(target) as z:
        if z.testzip() or set(z.namelist())!={'补货助手/'+n for n in files}:raise ValueError('ZIP inventory differs')
        if any(hashlib.sha256(z.read('补货助手/'+n)).hexdigest()!=h for n,h in files.items()):raise ValueError('ZIP SHA differs')
    result={'passed':True,'version':'0.15.0','baseline_commit':load(Q/'baseline.json')['commit'],'build_commit':head,'branch':subprocess.check_output(['git','branch','--show-current'],cwd=ROOT,text=True).strip(),'model_version':'0.3.3-rules','routing_version':'0.5.0','job_storage_format':2,'lifecycle_schema':1,'exe_sha256':sha(APP/'补货助手.exe'),'zip_sha256':sha(target),'file_count':len(files),'protected_files':len(protected),'protected_changes':changes,'unchanged_business_files':hashes,'business_field_changes':[],'allowed_changes':['document-derived home progress and grouped lifecycle UI','fixed mirror names; no manual follow-up controls','new independent lifecycle versions/files/events/mappings','new downstream exports; existing exporters unchanged','app build metadata'],'backup_side_effects':['original first-write safety backup','snapshot_chunks and snapshot_storage_meta may be created by existing backup mechanism'],'deferred':['security dependency upgrade'],'limitations':['Real external paid/receipt samples not supplied: generated templates, mappings and synthetic lifecycle fixtures verified','First row headers; equal-header sheets read together; different-header sheets need explicit mapping; no heuristic unit/status/revision inference','One complete external document per warehouse order stage, SKU-level duplicate conflicts reject rather than infer splits','Old EXE does not display new lifecycle tables; original jobs/signing/feedback/purchase rows remain unchanged','Source UI offscreen font rendering is not native proof; separate actual EXE/native Windows evidence supplied','Performance uses generated 1000-warehouse-order/5000-line fixtures on this PC; no all-machine promise'],'no_remote_actions':True}
    save(REL/'release_report.json',result)
    for p in REL.iterdir():
        if p.suffix in ('.json','.txt','.md'):p.write_bytes(p.read_bytes().replace(b'\r\n',b'\n'))
    print(json.dumps({k:result[k] for k in ('passed','build_commit','file_count','protected_files','exe_sha256','zip_sha256')}))
if __name__=='__main__':main()
