"""Seal exact tested 0.16.0 build; no remote actions or user data writes."""
import hashlib,json,shutil,subprocess,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];Q=ROOT/'qa/reconciliation_016';DIST=ROOT/'交付_0.16.0_rc2';APP=DIST/'补货助手';REL=ROOT/'releases/0.16.0/rc2'
def sha(p):
    h=hashlib.sha256()
    with Path(p).open('rb') as f:
        for b in iter(lambda:f.read(4*1024*1024),b''):h.update(b)
    return h.hexdigest()
def load(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def save(p,v):Path(p).write_bytes((json.dumps(v,ensure_ascii=False,indent=2)+'\n').encode())
def main():
    commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip();build=load(APP/'_internal/build_info.json')
    if build['source_commit']!=commit or build['version']!='0.16.0':raise ValueError('Seal at exact build commit')
    if subprocess.check_output(['git','status','--porcelain'],cwd=ROOT):raise ValueError('Expected clean checkout')
    reports={'business-equivalence.json':Q/'business-equivalence.json','matrix.json':Q/'matrix.json','exports.json':Q/'exports/report.json','source-ui.json':Q/'source-ui-1/report.json','exe-reconciliation.json':Q/'exe-reconciliation/report.json','exe-existing.json':Q/'exe-existing/report.json','performance.json':Q/'performance-final/report.json'}
    for name,p in reports.items():
        if not load(p)['passed']:raise ValueError('Evidence failed '+name)
    for name in ('exe-reconciliation.json','exe-existing.json'):
        if any(r['development'] or r['exit_code'] or r['remaining_children'] or r['exe_sha256']!=sha(APP/'补货助手.exe') or r['version']['source_commit']!=commit for r in load(reports[name])['runs']):raise ValueError('Native EXE/source mismatch')
    for name in ('units.txt','optimized-tests.txt'):
        if not (Q/name).read_text(encoding='utf-8-sig',errors='replace').rstrip().endswith('OK'):raise ValueError('Tests failed '+name)
    protected=load(Q/'protected.json');changes=[p for p,h in protected.items() if not Path(p).is_file() or sha(p)!=h]
    if changes:save(Q/'protected-changes.json',changes);raise ValueError('Protected files changed')
    core=['model.py','allocation.py','one_box_exception.py','default_config.json','default_must_sell.json','engine.py','input_adapter.py','batch_service.py','warehouse_catalog.py','new_products.py','result_semantics.py','parameter_schema.py','normal_exports.py','archive_batch.py','archive_exports.py','xlsx_exports.py','merchant_feedback.py','storage_chunks.py','work_repository.py','normal_orders.py','normal_delivery.py','work_io_process.py','history_forms.py','source_archive.py']
    hashes={n:{'baseline':sha(Q/'baseline/src'/n),'candidate':sha(ROOT/'src'/n)} for n in core}
    if any(h['baseline']!=h['candidate'] for h in hashes.values()):raise ValueError('Core business implementation changed')
    if sha(ROOT/'requirements-win-amd64.lock')!=sha(Q/'baseline/requirements-win-amd64.lock'):raise ValueError('Deferred dependencies changed')
    frozen=load(ROOT/'qa/parallel_062/frozen.json');input_changes=[p for p,h in frozen['inputs'].items() if not Path(p).is_file() or sha(p)!=h]
    if input_changes:raise ValueError('Original regression frozen inputs changed')
    REL.mkdir(parents=True,exist_ok=False)
    for name,p in reports.items():shutil.copy2(p,REL/name)
    for name in ('baseline.json','environment.json','units.txt','optimized-tests.txt'):shutil.copy2(Q/name,REL/name)
    save(REL/'input-hashes.json',frozen['inputs'])
    for name in ('home-exercise.png','lifecycle-form.png','preview-payment-save.png','preview-return-save.png'):shutil.copy2(Q/'exe-reconciliation'/name,REL/name)
    files={p.relative_to(APP).as_posix():sha(p) for p in APP.rglob('*') if p.is_file()};save(REL/'files.json',files);target=DIST/'补货助手_0.16.0_rc2.zip'
    with zipfile.ZipFile(target,'x',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for name in files:z.write(APP/name,'补货助手/'+name)
    with zipfile.ZipFile(target) as z:
        if z.testzip() or set(z.namelist())!={'补货助手/'+n for n in files} or any(hashlib.sha256(z.read('补货助手/'+n)).hexdigest()!=h for n,h in files.items()):raise ValueError('ZIP differs')
    result={'passed':True,'version':'0.16.0','baseline_commit':load(Q/'baseline.json')['commit'],'build_commit':commit,'branch':subprocess.check_output(['git','branch','--show-current'],cwd=ROOT,text=True).strip(),'model_version':'0.3.3-rules','lifecycle_schema':2,'job_storage_format':2,'exe_sha256':sha(APP/'补货助手.exe'),'zip_sha256':sha(target),'file_count':len(files),'protected_files':len(protected),'protected_changes':changes,'unchanged_business_files':hashes,'original_business_differences':0,'allowed_changes':['new lifecycle payload schema/rule/basis/dependencies/raw_rows/pending/explicit_zero','effective lifecycle quantities for blank/missing rows and predecessor-positive reference scope','new lifecycle per-row provenance/upstream_quantity/difference/outside_basis/processing_note','new payment adapter original metadata, no payment timestamp, optional source currency','new return export preserves signed layout with confirmed quantities; new payment/receipt export audit columns','document-derived progress and explicit recheck/zero actions; grouped imports/exports','application build metadata'],'old_expectation_updates':['blank quantities now accepted and kept as blank originals, effective zero after confirmation','payment source currency absence kept unknown; actual payment time not recorded','out-of-order documents need explicit recheck when upstream arrives','payment minimum template uses existing 13-column format; new payment export omits payment date and adds audit fields'],'limitations':['Payment ERP screenshot only: same-header synthetic workbooks verified; actual original ERP Excel not supplied','Payment multi-order import/automatic store mapping deferred; one complete selected warehouse order per import','Return combined export rejects multiple revisions of the same warehouse order to avoid ambiguous identical order numbers','Old lifecycle schema1 read unchanged; new schema2 requires 0.16; older EXE must use its old data/backup for rollback','No dependency/security upgrade; no external publication/merge/push','Performance is generated 5000 rows per stage, same PC and warm OS cache; extra audit work may cost time/memory'],'no_remote_actions':True}
    save(REL/'release_report.json',result)
    for p in REL.iterdir():
        if p.suffix in ('.json','.txt','.md'):p.write_bytes(p.read_bytes().replace(b'\r\n',b'\n'))
    print(json.dumps({k:result[k] for k in ('passed','build_commit','file_count','protected_files','exe_sha256','zip_sha256')}))
if __name__=='__main__':main()
