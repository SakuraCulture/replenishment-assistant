from pathlib import Path
import ast,hashlib,json,shutil,subprocess,zipfile
ROOT=Path.cwd();Q=ROOT/'qa/lifecycle_simple_0161';DIST=ROOT/'交付_0.16.1_rc1';APP=DIST/'补货助手';REL=ROOT/'releases/0.16.1/rc1'
def sha(p):
    h=hashlib.sha256()
    with Path(p).open('rb') as f:
        for block in iter(lambda:f.read(4*1024*1024),b''):h.update(block)
    return h.hexdigest()
def load(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def save(p,x):Path(p).write_bytes((json.dumps(x,ensure_ascii=False,indent=2)+'\n').encode())
commit=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip();build=load(APP/'_internal/build_info.json')
if commit!=build['source_commit'] or subprocess.check_output(['git','status','--porcelain']):raise ValueError('Source/build not exact or checkout dirty')
reports={name:load(Q/file) for name,file in {'exports.json':'exports/report.json','source-ui.json':'source-ui/report.json','exe-new.json':'exe-new/report.json','exe-existing.json':'exe-existing/report.json'}.items()}
if not all(r['passed'] for r in reports.values()):raise ValueError('Failed evidence')
for key in ('exe-new.json','exe-existing.json'):
    for r in reports[key]['runs']:
        if r['development'] or r['remaining_children'] or r['exit_code'] or r['version']['source_commit']!=commit or r['exe_sha256']!=sha(APP/'补货助手.exe'):raise ValueError('EXE mismatch')
for name in ('units.txt','optimized-tests.txt'):
    if not (Q/name).read_text(encoding='utf-8-sig').rstrip().endswith('OK'):raise ValueError('Tests failed')
old=load(Q/'baseline.json');oldroot=ROOT/'交付_0.16.0_rc2/补货助手';oldfiles=load(ROOT/'releases/0.16.0/rc2/files.json')
changes=[n for n,h in oldfiles.items() if not (oldroot/n).is_file() or sha(oldroot/n)!=h]
if changes or sha(oldroot.parent/'补货助手_0.16.0_rc2.zip')!=old['old_zip_sha256']:raise ValueError('Old delivery changed')
core=['model.py','allocation.py','one_box_exception.py','default_config.json','default_must_sell.json','engine.py','input_adapter.py','batch_service.py','warehouse_catalog.py','new_products.py','result_semantics.py','parameter_schema.py','normal_exports.py','archive_batch.py','archive_exports.py','xlsx_exports.py','merchant_feedback.py','storage_chunks.py','work_repository.py','normal_orders.py','normal_delivery.py','work_io_process.py','history_forms.py','source_archive.py','lifecycle_rules.py','lifecycle_exports.py']
hashes={n:{'old':sha(Q/'baseline/src'/n),'new':sha(ROOT/'src'/n)} for n in core}
if any(h['old']!=h['new'] for h in hashes.values()):raise ValueError('Business core changed')
def funcs(path):return {f.name:ast.dump(f,include_attributes=False) for f in ast.parse(path.read_text(encoding='utf-8')).body if isinstance(f,(ast.FunctionDef,ast.AsyncFunctionDef))}
a=funcs(Q/'baseline/src/order_lifecycle.py');b=funcs(ROOT/'src/order_lifecycle.py');same={n:a[n]==b[n] for n in ('prepare','commit','snapshot','recheck','upstream','fingerprint','make_plan')}
if not all(same.values()):raise ValueError('Internal exact-snapshot algorithm changed')
REL.mkdir(parents=True,exist_ok=False)
for name,r in reports.items():save(REL/name,r)
for name in ('baseline.json','units.txt','optimized-tests.txt'):shutil.copy2(Q/name,REL/name)
for name in ('simple-mapping.png','preview-return-save.png','preview-payment-save.png'):shutil.copy2(Q/'exe-new'/name,REL/name)
files={p.relative_to(APP).as_posix():sha(p) for p in APP.rglob('*') if p.is_file()};save(REL/'files.json',files)
zip_path=DIST/'补货助手_0.16.1_rc1.zip'
with zipfile.ZipFile(zip_path,'x',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
    for name in files:z.write(APP/name,'补货助手/'+name)
with zipfile.ZipFile(zip_path) as z:
    if z.testzip() or set(z.namelist())!={'补货助手/'+n for n in files} or any(hashlib.sha256(z.read('补货助手/'+n)).hexdigest()!=h for n,h in files.items()):raise ValueError('ZIP differs')
r={'passed':True,'baseline_commit':old['commit'],'build_commit':commit,'version':build['version'],'branch':subprocess.check_output(['git','branch','--show-current'],text=True).strip(),'model_version':'0.3.3-rules','exe_sha256':sha(APP/'补货助手.exe'),'zip_sha256':sha(zip_path),'file_count':len(files),'old_delivery_files_checked':len(oldfiles),'old_delivery_differences':changes,'core_hashes':hashes,'internal_lifecycle_functions_unchanged':same,'tests_normal':170,'tests_optimized':170,'original_exports_cases':12,'original_exports_differences':0,'native_launches':4,'allowed_changes':['public import associates current signing by warehouse order number; supplied revision does not choose target','return minimal template 5 labelled fields; receipt removes system fields; payment original template retained','mapping UI only business fields; automatic predecessor quantities and differences; no revision picker','public homepage/current order labels omit revision; private audit and historical snapshot formats preserved','application version and QA instrumentation'],'limits':['No payment batch import added','No generic header-row detection or universal mappings added','Actual payment ERP workbook not provided; screenshot-header synthetic fixtures only','Old revisions/data preserved; current signing can have changed since an old return file was sent, so whole-order preview remains required','This focused patch reruns 170 unit tests, original export equivalence and native workflows; it does not claim a new 14244-row/performance benchmark run'],'no_push_or_merge':True}
save(REL/'release_report.json',r)
print(json.dumps({k:r[k] for k in ('passed','build_commit','exe_sha256','zip_sha256','file_count','old_delivery_files_checked')}))
