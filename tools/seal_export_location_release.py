"""Seal exact tested 0.15.1 candidate; preserve existing packages/configuration."""
import hashlib,json,shutil,subprocess,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];Q=ROOT/'qa/export_location_0151';DIST=ROOT/'交付_0.15.1_rc1';APP=DIST/'补货助手';REL=ROOT/'releases/0.15.1/rc1'
def sha(p):
    h=hashlib.sha256()
    with Path(p).open('rb') as f:
        for b in iter(lambda:f.read(4*1024*1024),b''):h.update(b)
    return h.hexdigest()
def load(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def save(p,v):Path(p).write_bytes((json.dumps(v,ensure_ascii=False,indent=2)+'\n').encode())
def main():
    commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip();build=load(APP/'_internal/build_info.json')
    if build['source_commit']!=commit or build['version']!='0.15.1':raise ValueError('Seal at exact build commit')
    if subprocess.check_output(['git','status','--porcelain'],cwd=ROOT):raise ValueError('Expected clean source checkout')
    reports={'exports.json':Q/'exports/report.json','source-ui.json':Q/'source-ui/report.json','exe-location.json':Q/'exe-location/report.json','exe-existing.json':Q/'exe-existing/report.json'}
    for name,p in reports.items():
        if not load(p)['passed']:raise ValueError('Failed evidence '+name)
    for name in ('exe-location.json','exe-existing.json'):
        if any(r['development'] or r['exit_code'] or r['remaining_children'] or r['exe_sha256']!=sha(APP/'补货助手.exe') or r['version']['source_commit']!=commit for r in load(reports[name])['runs']):raise ValueError('Actual EXE/source identity mismatch')
    for name in ('units.txt','optimized-tests.txt'):
        if not (Q/name).read_text(encoding='utf-8-sig',errors='replace').rstrip().endswith('OK'):raise ValueError('Test did not pass '+name)
    protected=load(Q/'protected.json');changed=[p for p,h in protected.items() if not Path(p).is_file() or sha(p)!=h]
    if changed:save(Q/'protected-changes.json',changed);raise ValueError('Protected input/package/configuration changed')
    core=['model.py','allocation.py','one_box_exception.py','engine.py','input_adapter.py','batch_service.py','normal_orders.py','normal_delivery.py','normal_exports.py','archive_batch.py','archive_exports.py','xlsx_exports.py','history_forms.py','merchant_feedback.py','order_lifecycle.py','lifecycle_io.py','purchases.py','default_config.json','default_must_sell.json','work_repository.py','storage_chunks.py']
    hashes={n:{'baseline':sha(Q/'baseline/src'/n),'candidate':sha(ROOT/'src'/n)} for n in core}
    if any(h['baseline']!=h['candidate'] for h in hashes.values()):raise ValueError('Business implementation changed')
    REL.mkdir(parents=True,exist_ok=False)
    for name,p in reports.items():shutil.copy2(p,REL/name)
    for name in ('baseline.json','units.txt','optimized-tests.txt','environment.json'):shutil.copy2(Q/name,REL/name)
    for name in ('settings-save.png','settings-reopen.png'):shutil.copy2(Q/'exe-location'/name,REL/name)
    files={p.relative_to(APP).as_posix():sha(p) for p in APP.rglob('*') if p.is_file()};save(REL/'files.json',files);target=DIST/'补货助手_0.15.1_rc1.zip'
    with zipfile.ZipFile(target,'x',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for name in files:z.write(APP/name,'补货助手/'+name)
    with zipfile.ZipFile(target) as z:
        if z.testzip() or set(z.namelist())!={'补货助手/'+n for n in files} or any(hashlib.sha256(z.read('补货助手/'+n)).hexdigest()!=h for n,h in files.items()):raise ValueError('ZIP content mismatch')
    report={'passed':True,'version':'0.15.1','baseline_commit':load(Q/'baseline.json')['commit'],'build_commit':commit,'branch':subprocess.check_output(['git','branch','--show-current'],cwd=ROOT,text=True).strip(),'model_version':'0.3.3-rules','exe_sha256':sha(APP/'补货助手.exe'),'zip_sha256':sha(target),'file_count':len(files),'protected_files':len(protected),'protected_changes':changed,'unchanged_business_files':hashes,'allowed_changes':['local export_settings.json preference','save-dialog initial directory and settings form','application build metadata'],'business_field_changes':[],'limitations':['Native Windows UI automated on this computer; network/removable path access depends on OS availability','Configuration file is created only on explicit Save; actual user default has not been preselected','No fresh 14244-row model rerun for this UI-only change; 21 core business files byte-identical and all 145 portable tests run','Dependency/security changes remain deferred'],'no_remote_actions':True}
    save(REL/'release_report.json',report)
    for p in REL.iterdir():
        if p.suffix in ('.txt','.md','.json'):p.write_bytes(p.read_bytes().replace(b'\r\n',b'\n'))
    print(json.dumps({k:report[k] for k in ('passed','build_commit','file_count','exe_sha256','zip_sha256')}))
if __name__=='__main__':main()
