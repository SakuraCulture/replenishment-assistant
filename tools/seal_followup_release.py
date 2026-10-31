"""Seal 0.14.0 only after strict frozen business/export and real EXE checks."""
import hashlib,json,shutil,subprocess,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];Q=ROOT/'qa/followup_014';DIST=ROOT/'交付_0.14.0_rc2';APP=DIST/'补货助手';REL=ROOT/'releases/0.14.0/rc2'

def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(4*1024*1024),b''):h.update(block)
    return h.hexdigest()
def load(path):return json.loads(Path(path).read_text(encoding='utf-8-sig'))
def save(path,value):Path(path).write_bytes((json.dumps(value,ensure_ascii=False,indent=2)+'\n').encode())

def main():
    head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip();build=load(APP/'_internal/build_info.json')
    if head!=build['source_commit'] or build['version']!='0.14.0':raise ValueError('Seal at exact 0.14.0 build commit')
    if subprocess.check_output(['git','status','--porcelain'],cwd=ROOT):raise ValueError('Expected clean build checkout')
    reports={'business-equivalence.json':Q/'business-equivalence.json','archive-exports.json':Q/'exports/report.json','matrix.json':Q/'matrix.json','source-ui.json':Q/'source-final/report.json','exe-followup.json':Q/'exe-followup/report.json','exe-existing.json':Q/'exe-existing/report.json','scale.json':Q/'scale-final/report.json'}
    for name,path in reports.items():
        if not load(path)['passed']:raise ValueError('Failed evidence: '+name)
    for name in ('exe-followup.json','exe-existing.json'):
        if any(r['development'] or r['exit_code'] or r['remaining_children'] or r['exe_sha256']!=sha(APP/'补货助手.exe') or r['version']['source_commit']!=head for r in load(reports[name])['runs']):raise ValueError('EXE binding mismatch: '+name)
    for name in ('units.txt','optimized-tests.txt'):
        if not (Q/name).read_text(encoding='utf-8-sig',errors='replace').rstrip().endswith('OK'):raise ValueError('Tests did not finish OK: '+name)
    protected={**load(Q/'protected.json'),**load(Q/'inputs.json')};changed=[p for p,h in protected.items() if not Path(p).is_file() or sha(p)!=h]
    if changed:raise ValueError('Protected input/package changed: '+repr(changed))
    core=['model.py','allocation.py','one_box_exception.py','default_config.json','default_must_sell.json','engine.py','input_adapter.py','batch_service.py','warehouse_catalog.py','new_products.py','result_semantics.py','parameter_schema.py','normal_exports.py','archive_batch.py','archive_exports.py','xlsx_exports.py','merchant_feedback.py','storage_chunks.py','work_repository.py','normal_orders.py','normal_delivery.py','work_io_process.py','history_forms.py']
    hashes={n:{'baseline':sha(Q/'baseline/src'/n),'candidate':sha(ROOT/'src'/n)} for n in core}
    if any(v['baseline']!=v['candidate'] for v in hashes.values()):raise ValueError('Business implementation changed')
    if sha(ROOT/'requirements-win-amd64.lock')!=sha(Q/'baseline/requirements-win-amd64.lock'):raise ValueError('Deferred dependency upgrade must not be included')
    if (REL/'release_report.json').exists():raise ValueError('Candidate already sealed')
    REL.mkdir(parents=True,exist_ok=True)
    for name,path in reports.items():shutil.copy2(path,REL/name)
    for name in ('baseline.json','inputs.json','units.txt','optimized-tests.txt','environment.json'):shutil.copy2(Q/name,REL/name)
    for name in ('home-linked.png','purchase-preview-save.png','purchase-differences-save.png','execution-comparison.png','purchase-version.png'):shutil.copy2(Q/'exe-followup'/name,REL/name)
    files={p.relative_to(APP).as_posix():sha(p) for p in APP.rglob('*') if p.is_file()};save(REL/'files.json',files);target=DIST/'补货助手_0.14.0_rc2.zip'
    with zipfile.ZipFile(target,'x',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for name in files:z.write(APP/name,'补货助手/'+name)
    with zipfile.ZipFile(target) as z:
        if z.testzip() or set(z.namelist())!={'补货助手/'+n for n in files}:raise ValueError('ZIP contents differ')
        if any(hashlib.sha256(z.read('补货助手/'+n)).hexdigest()!=h for n,h in files.items()):raise ValueError('ZIP SHA mismatch')
    result={'passed':True,'version':'0.14.0','baseline_commit':load(Q/'baseline.json')['commit'],'build_commit':head,'branch':subprocess.check_output(['git','branch','--show-current'],cwd=ROOT,text=True).strip(),'model_version':'0.3.3-rules','routing_version':'0.5.0','storage_format_version':2,'purchase_payload_schema':1,'exe_sha256':sha(APP/'补货助手.exe'),'zip_sha256':sha(target),'file_count':len(files),'protected_count':len(protected),'protected_changes':changed,'unchanged_business_files':hashes,'business_field_changes':[],'allowed_changes':['new home projection and explicit follow-up notes','new independent purchase fact/version/source/link tables','new standalone execution comparison export, existing exporters unchanged','new home/history UI entrypoints and application build metadata'],'deferred':['security dependency upgrade','receipt linkage','effect analysis'],'limitations':['Actual external ERP files not supplied: standard template, configurable mapping and synthetic cases verified only','First row must contain headers; units and status require explicit interpretation','Purchase library and relevant historical revisions may grow; service scale measurements are not all-machine latency promises','Old EXE cannot display new purchase data; old signed/job tables remain unchanged','Actual EXE UI automation on this Windows host, not every Windows/DPI configuration'],'no_remote_actions':True}
    save(REL/'release_report.json',result)
    for path in REL.iterdir():
        if path.suffix in ('.json','.txt','.md'):path.write_bytes(path.read_bytes().replace(b'\r\n',b'\n'))
    print(json.dumps({k:result[k] for k in ('passed','build_commit','file_count','protected_count','exe_sha256','zip_sha256')}))
if __name__=='__main__':main()
