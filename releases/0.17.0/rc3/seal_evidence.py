"""Seal this local candidate after all verification; never updates old deliveries."""
from pathlib import Path
import hashlib,json,platform,shutil,subprocess,sys,zipfile
ROOT=Path.cwd();Q=ROOT/'qa/business_flows_0170';DIST=ROOT/'交付_0.17.0_rc3';APP=DIST/'补货助手';REL=ROOT/'releases/0.17.0/rc3'
def sha(p):
    h=hashlib.sha256()
    with Path(p).open('rb') as f:
        for block in iter(lambda:f.read(4*1024*1024),b''):h.update(block)
    return h.hexdigest()
def load(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def save(p,x):Path(p).write_bytes((json.dumps(x,ensure_ascii=False,indent=2)+'\n').encode('utf-8'))
def git(*a):return subprocess.check_output(['git',*a],text=True,encoding='utf-8').strip()
commit=git('rev-parse','HEAD');build=load(APP/'_internal/build_info.json');baseline=load(Q/'baseline.json')
if git('status','--porcelain') or build['source_commit']!=commit:raise ValueError('Build/source must match a clean commit')
reports={name:load(Q/source) for name,source in {'exports.json':'export_equivalence/report.json','lifecycle.json':'lifecycle_equivalence_final/report.json','parallel-schedules.json':'parallel_schedules/report.json','actual-exe.json':'exe-rc3/report.json'}.items()}
if not all(r['passed'] for r in reports.values()):raise ValueError('Verification failed')
for r in reports['actual-exe.json']['runs']:
    if r['source_only'] or r['exit_code'] or r['remaining_children'] or r['exe_sha256']!=sha(APP/'补货助手.exe') or r['version']['source_commit']!=commit:raise ValueError('Actual EXE evidence mismatch')
for name in ('units.txt','optimized-tests.txt'):
    s=(Q/name).read_text(encoding='utf-8-sig')
    if not s.rstrip().endswith('OK') or 'Ran 185 tests' not in s:raise ValueError('Unit checks failed')
model=load(Q/'model_equivalence.json');raw=[(c['case'],d) for c in model['cases'] for d in c['differences']]
if raw!=[('dual_parallel',['$','dictionary key order',"['QA_B', 'QA_A']","['QA_A', 'QA_B']"])]:raise ValueError('Unexpected model difference; do not expand whitelist')
if any(h['before']!=h['after'] or sha(ROOT/'src'/n)!=h['after'] for n,h in model['core_hashes'].items()):raise ValueError('Calculation/export core drift')
oldroot=ROOT/'交付_0.16.1_rc1/补货助手';old_changes=[n for n,h in baseline['old_files'].items() if not (oldroot/n).is_file() or sha(oldroot/n)!=h]
if old_changes or sha(oldroot.parent/'补货助手_0.16.1_rc1.zip')!=baseline['old_zip_sha256']:raise ValueError('Old delivery changed')
user=Path(baseline['user_folder']);user_changes=[n for n,h in baseline['user_files'].items() if not (user/n).is_file() or sha(user/n)!=h]
if user_changes:raise ValueError('Daily user data changed: '+str(user_changes))
input_hashes={p.name:sha(p) for p in (Q/'inputs').iterdir() if p.is_file()}
source_inputs=ROOT/'qa/structure_061/inputs'
input_changes=[n for n,h in input_hashes.items() if not (source_inputs/n).is_file() or sha(source_inputs/n)!=h]
if input_changes:raise ValueError('Frozen inputs changed: '+str(input_changes))
# Verify the three original user workbooks too, using the original frozen manifest.
original=load(ROOT/'qa/structure_061/frozen.json')['original_inputs']
original_checks={p:{'exists':Path(p).is_file(),'sha256':sha(p) if Path(p).is_file() else None,'frozen_sha256':h} for p,h in original.items()}
if any(not v['exists'] or v['sha256']!=v['frozen_sha256'] for v in original_checks.values()):raise ValueError('Original input no longer matches historical frozen source; investigate')
REL.mkdir(parents=True,exist_ok=False)
for name,r in reports.items():save(REL/name,r)
for name in ('baseline.json','units.txt','optimized-tests.txt','model_equivalence.json','model_uncontrolled_order.json','build-rc3.log'):shutil.copy2(Q/name,REL/name)
for name in ('model_old_fixed','model_new_fixed'):shutil.copy2(Q/name/'capture.json',REL/(name+'-source.json'))
for name in ('layout-1380-0.png','layout-1080-2.png','layout-1380-3.png','decision-review.png','batch-save.png','parameters.png'):shutil.copy2(Q/'exe-rc3'/name,REL/name)
shutil.copy2(__file__,REL/'seal_evidence.py')
files={p.relative_to(APP).as_posix():sha(p) for p in APP.rglob('*') if p.is_file()};save(REL/'files.json',files)
zip_path=DIST/'补货助手_0.17.0_rc3.zip'
with zipfile.ZipFile(zip_path,'x',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
    for name in files:z.write(APP/name,'补货助手/'+name)
with zipfile.ZipFile(zip_path) as z:
    if z.testzip() or set(z.namelist())!={'补货助手/'+n for n in files} or any(hashlib.sha256(z.read('补货助手/'+n)).hexdigest()!=h for n,h in files.items()):raise ValueError('ZIP differs')
result={'passed':True,'baseline_commit':baseline['commit'],'build_commit':commit,'branch':git('branch','--show-current'),'version':'0.17.0','model_version':'0.3.3-rules','build_id':'rc3','exe_sha256':sha(APP/'补货助手.exe'),'zip_sha256':sha(zip_path),'file_count':len(files),'tests_normal':185,'tests_optimized':185,'original_export_cases':12,'original_export_differences':0,'model_capture_cases':30,'model_business_value_or_product_order_differences':0,'raw_model_completion_order_differences':raw,'controlled_parallel_orders_verified':['QA_A,QA_B','QA_B,QA_A'],'controlled_parallel_differences':0,'lifecycle_payload_and_exports_differences':0,'native_launches':2,'core_hashes':model['core_hashes'],'old_delivery_files_checked':len(baseline['old_files']),'old_delivery_differences':old_changes,'daily_user_files_checked':len(baseline['user_files']),'daily_user_differences':user_changes,'input_hashes':input_hashes,'original_inputs':original_checks,'source_hashes':{p.name:sha(p) for p in (ROOT/'src').iterdir() if p.is_file()},'environment':{'python':sys.version,'platform':platform.platform()},'allowed_changes':['navigation and readonly business forms','explicit batch lifecycle preparation and one transaction using unchanged quantity rules','optional compare-and-save protection for stored defaults','completion and partial-export receipts','application version and opt-in QA entry; model/routing/storage versions unchanged'],'limits':['No human usability study yet; regular signing still five necessary actions instead of target three','Payment original workbook not supplied; screenshot-header synthetic tests only','Global batch target chooser still reads available order set; long-history scale unmeasured','Old archives with missing attachments/audit fields remain missing; no historical algorithm replay','Natural process completion dictionary order is not deterministic in both versions; original difference retained, two controlled schedules and exported order match','Small four-product timing fixture; OS cache uncontrolled; do not extrapolate speedups','Local Windows EXE validated; clean-PC rebuild not newly validated'],'no_push_or_merge':True}
save(REL/'release_report.json',result)
print(json.dumps({k:result[k] for k in ('passed','build_commit','exe_sha256','zip_sha256','file_count','old_delivery_files_checked','daily_user_files_checked')}))
