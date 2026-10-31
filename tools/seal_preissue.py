"""Seal only the independently verified 0.18.0 rc2 candidate; never old packages."""
import copy,hashlib,json,shutil,subprocess,sys,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];QA=ROOT/'qa/preissue_0180';DIST=ROOT/'交付_0.18.0_rc2';APP=DIST/'补货助手';REL=ROOT/'releases/0.18.0/rc2'
def sha(p):
    with Path(p).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def read(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def save(p,v):Path(p).write_bytes((json.dumps(v,ensure_ascii=False,indent=2)+'\n').encode())
def git(*args):return subprocess.check_output(['git',*args],cwd=ROOT,text=True,encoding='utf-8').strip()
def main():
    if git('status','--porcelain'):raise ValueError('Require clean release preparation commit')
    build=read(APP/'_internal/build_info.json');commit=build['source_commit']
    if git('diff',commit,'--','src','prepare_delivery.py','requirements-win-amd64.lock'):raise ValueError('Runtime differs from actual EXE build')
    baseline=read(QA/'baseline.json');core={n:sha(ROOT/'src'/n) for n in baseline['core']}
    if core!=baseline['core']:raise ValueError('Protected calculation/defaults changed')
    preserved={**baseline['protected'],**baseline['inputs']}
    if any(sha(p)!=h for p,h in preserved.items()):raise ValueError('Protected input, old delivery or user data changed')
    eq=read(QA/'equivalence-final.json');parallel=read(QA/'parallel-equivalence/report.json');exports=read(QA/'archive-equivalence/report.json')
    differences=[c for c in eq['cases'] if c['differences']]
    # Keep the raw mismatch in the report. Only the pre-existing completion-order
    # variation can be resolved by the separately executed two exact schedules.
    if differences and not (len(differences)==1 and differences[0]['name']=='dual_parallel' and len(differences[0]['differences'])==1 and differences[0]['differences'][0][:2]==['$','dictionary key order'] and parallel['passed']):raise ValueError('Unresolved strict equivalence difference')
    if not exports['passed'] or not parallel['passed']:raise ValueError('Export/parallel acceptance failed')
    exe_reports={name:read(QA/folder/'report.json') for name,folder in [('actual-exe','actual-exe'),('legacy-exe','legacy-exe')]}
    for report in exe_reports.values():
        if not report['passed']:raise ValueError('EXE acceptance failed')
        for run in report['runs']:
            if run['source_only'] or run['exit_code'] or run['remaining_children'] or run['exe_sha256']!=sha(APP/'补货助手.exe') or run['version']['source_commit']!=commit:raise ValueError('EXE evidence mismatches candidate')
    tests={}
    for name in ('units','optimized'):
        summary=(QA/(name+'.txt')).read_text(encoding='utf-8-sig');path=Path(summary.split('本轮记录：')[-1].strip())/'tests.txt';text=path.read_text(encoding='utf-8-sig')
        if 'Ran 254 tests' not in text or not text.rstrip().endswith('OK'):raise ValueError('Unit evidence failed')
        tests[name]=path
    sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tools')]
    import preissue,result_semantics as sem,work_repository as wr
    from parallel_qa import read as read_result,diff,clean
    r=read_result(QA/'model_old/real_None.pklz');selected=[d for d in r['rows'] if not preissue.blocked(d) and sem.order_quantity(d)>0][:10]
    edits={d['E']:{'quantity':0,'kind':'manual','reason':'合成验收：人工不补'} for d in selected};v=preissue.preview(r,edits,wr.today());after=preissue.finalize(v,[x['id'] for x in v['risks']]);preissue.validate_result(after,wr.today())
    if diff(preissue.original(after),r):raise ValueError('Original result restoration mismatch')
    allowed=set(preissue.EXEC_FIELDS)|{'_preissue'};unexpected=[];changes=[]
    if [(d['B'],d['E']) for d in r['rows']]!=[(d['B'],d['E']) for d in after['rows']]:raise ValueError('Row identity/order changed')
    for old,new in zip(r['rows'],after['rows']):
        changed=diff(old,new)
        if old['E'] not in edits:unexpected.extend(changed)
        else:
            unexpected.extend(diff({k:x for k,x in old.items() if k not in allowed},{k:x for k,x in new.items() if k not in allowed}))
            changes.append({'sku':old['E'],'original_quantity':sem.order_quantity(old),'final_quantity':sem.order_quantity(new),'original_amount':sem.order_amount(old),'final_amount':sem.order_amount(new),'changed_fields':sorted(k for k in set(old)|set(new) if k not in old or k not in new or type(old[k])!=type(new[k]) or old[k]!=new[k])})
    if unexpected:raise ValueError('Unselected/intermediate fields changed')
    adjustment={'passed':True,'synthetic_edits_on_real_14244_rows':True,'row_count':len(r['rows']),'changes':changes,'unexpected':unexpected,'allowed_row_fields':sorted(allowed),'allowed_root_fields':['preissue','summary','budget'],'original_restoration_differences':[],'row_identity_order_unchanged':True}
    REL.mkdir(parents=True,exist_ok=False)
    for name,value in [('baseline',baseline),('equivalence-final',eq),('parallel-equivalence',parallel),('archive-equivalence',exports),('adjusted-real',adjustment),('performance',read(QA/'performance/report.json')), *exe_reports.items()]:save(REL/(name+'.json'),value)
    shutil.copy2(QA/'equivalence.json',REL/'first-equivalence-retained.json')
    for name,path in tests.items():shutil.copy2(path,REL/(name+'.txt'))
    # Only the release guide link is updated after compilation; runtime is still
    # byte-for-byte bound to build_commit. Record the documentation commit too.
    guide=ROOT/'docs/0.18.0签发前最终清单.md';shutil.copy2(guide,APP/'docs'/guide.name);shutil.copy2(guide,APP/'使用说明.txt')
    files={p.relative_to(APP).as_posix():sha(p) for p in APP.rglob('*') if p.is_file()};save(REL/'files.json',files)
    target=DIST/'补货助手_0.18.0_rc2.zip'
    with zipfile.ZipFile(target,'x',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for name in files:z.write(APP/name,'补货助手/'+name)
    with zipfile.ZipFile(target) as z:
        if set(z.namelist())!={'补货助手/'+n for n in files} or any(hashlib.sha256(z.read('补货助手/'+n)).hexdigest()!=h for n,h in files.items()):raise ValueError('ZIP contents differ')
    report={'passed':True,'baseline_commit':baseline['commit'],'build_commit':commit,'documentation_commit':git('rev-parse','HEAD'),'branch':git('branch','--show-current'),'version':'0.18.0','model_version':'0.3.3-rules','exe_sha256':sha(APP/'补货助手.exe'),'zip_sha256':sha(target),'file_count':len(files),'protected_files_verified':len(preserved),'core_hashes':core,'normal_tests':254,'optimized_tests':254,'strict_cases':len(eq['cases']),'raw_differences':differences,'controlled_parallel_passed':parallel['passed'],'export_cases':len(exports['cases']),'exe_launches':sum(len(r['runs']) for r in exe_reports.values()),'no_push_or_merge':True,'adjustment_evidence':'adjusted-real.json','notes':['Model/defaults unchanged; only explicit final edits alter quantities and corresponding execution audit.','Original candidate rc1 was not delivered; rc2 includes strict focus-value validation.','No cross-machine build or human usability claim.','Old EXEs must not edit or re-sign new final-adjustment drafts.','No self-generated input copies included in the application ZIP.']}
    save(REL/'release_report.json',report);print(json.dumps(report,ensure_ascii=False))
if __name__=='__main__':main()
