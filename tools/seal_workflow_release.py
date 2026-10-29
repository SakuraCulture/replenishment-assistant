"""Seal 0.7.3 from a clean, independently verified candidate; never overwrite old delivery."""
import argparse,hashlib,json,shutil,subprocess,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def sha(path):
    with Path(path).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def load(path):return json.loads(Path(path).read_text(encoding='utf-8'))

def main():
    p=argparse.ArgumentParser();p.add_argument('--delivery',type=Path,required=True);p.add_argument('--qa',type=Path,required=True);a=p.parse_args();dest=a.delivery.resolve();q=a.qa.resolve();meta=load(dest/'版本信息.json');commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    build_commit=meta['source_commit'];assert meta['version']=='0.7.3'
    subprocess.run(['git','merge-base','--is-ancestor',build_commit,commit],cwd=ROOT,check=True)
    after_build=[n for n in subprocess.check_output(['git','diff','--name-only','-z',build_commit,commit],cwd=ROOT).decode('utf-8').split('\0') if n]
    assert all(name.startswith(('tools/','docs/','releases/')) or name in ('README.md','使用说明.txt','prepare_delivery.py') for name in after_build),'runtime/build files changed since clean build'
    assert not subprocess.check_output(['git','status','--porcelain'],cwd=ROOT,text=True).strip(),'clean build commit required'
    paths={'business':q/'backend_equivalence.json','goods':q/'goods_parity.json','new_products':q/'new_product_parity.json','real_exports':q/'exports_real/report.json','batch_exports':q/'exports_synthetic_retry/report.json','io_controls':q/'io_controls/report.json','transfer':q/'transfer_final/report.json','pressure_equivalence':q/'pressure_all_gc/equivalence.json','normal_backend':q/'normal_release/normal_report.json','workbench_backend':q/'workbench_release/workbench_report.json'}
    ui={'normal_ui':q/'exe-normal/normal_ui_report.json','work_ui':q/'exe-work/work_ui_report.json','delivery_ui':q/'exe-delivery/export_ui_report.json','history_ui':q/'exe-history/history_ui_report.json','large_work_ui':q/'exe-work-io/work_io_report.json','pressure_ui':q/'exe-pressure/workflow_report.json'}
    paths['frozen_equivalence']=q/'exe-pressure/equivalence.json'
    evidence={}
    for name,path in {**paths,**ui}.items():
        data=load(path);assert data['passed'],name;evidence[name]=data
        if name in ui:
            assert not data.get('development') and data['exe_sha256']==sha(dest/'补货助手.exe'),name
            version=data['version'];built=version['source_commit'] if isinstance(version,dict) else data['source_commit'];assert built==build_commit,(name,'stale EXE')
    source={str(p.relative_to(ROOT)):sha(p) for p in sorted((ROOT/'src').glob('*')) if p.is_file()};assert evidence['business']['candidate_sources']==source,'source changed after independent business capture'
    core=['model.py','one_box_exception.py','allocation.py','default_config.json','default_must_sell.json','work_repository.py','normal_orders.py','new_products.py','engine.py','batch_service.py','warehouse_catalog.py','input_adapter.py','table_records.py','parameter_schema.py']
    unchanged={n:sha(ROOT/'src'/n)==sha(q/'baseline/src'/n) for n in core};assert all(unchanged.values())
    protected=load(q/'protected_hashes.json');assert all(sha(ROOT/n)==h for n,h in protected.items())
    original_files=load(q/'inputs_manifest.json');assert all(sha(r['path'])==r['sha256'] for r in original_files)
    user=load(q/'user_data_check.json');assert sha(user['user_database'])==user['sha256'];assert user['matches_prior_verified_hash']
    ui_perf=load(q/'ui_bench_verified/report.json');large_perf=load(q/'large_bench_verified/report.json');assert large_perf['passed'] and large_perf['runs']==3
    assert all(len(list((q/'ui_bench_verified').glob(kind+'-*.json')))==3 for kind in ('old','new'))
    out=ROOT/'releases/0.7.3'/meta['build_id'];out.mkdir(parents=True,exist_ok=False)
    for name,path in {**paths,**ui,'ui_performance':q/'ui_bench_verified/report.json','large_performance':q/'large_bench_verified/report.json','source_pressure':q/'pressure_all_gc/workflow_report.json','source_large_work':q/'work_io_source/work_io_report.json','environment':q/'environment.json','user_data_check':q/'user_data_check.json','input_manifest':q/'inputs_manifest.json'}.items():shutil.copy2(path,out/(name+'.json'))
    receipt={'version':'0.7.3','source_commit':commit,'build_id':meta['build_id'],'baseline_commit':'84a0671bd81daa8c2e94a8648c2332b9fc8fa2a9','passed':True,'unchanged_business_sources':{n:{'sha256':sha(ROOT/'src'/n),'unchanged':ok} for n,ok in unchanged.items()},'old_package_and_original_inputs_unchanged':True,'daily_user_db_unchanged':True,'exe_sha256':sha(dest/'补货助手.exe'),'business_delta':'Only authorized work/product-Excel exclusion of disabled/stopped rows; full raw archive and calculation rows retained','limits':['Daily storage format unchanged; full snapshot save/sign/read still takes time and memory, now separated from UI thread','Same-machine source benchmark and actual EXE acceptance; no clean-machine or byte-reproducible-build claim','25-file stress and exact per-store outputs measured; additional entities use explicitly labelled QA standard parameters','History external confirmation/order/receipt linking remains outside this work','No remote push, merge or replacement of existing delivery'],'delivery':str(dest)}
    receipt.update(source_commit=build_commit,evidence_commit=commit,post_build_nonruntime_changes=after_build)
    (dest/'验收记录.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8');files={p.relative_to(dest).as_posix():sha(p) for p in dest.rglob('*') if p.is_file()};(out/'files_sha256.json').write_text(json.dumps(files,ensure_ascii=False,indent=2),encoding='utf-8')
    archive=dest.parent/f"补货助手_0.7.3_{meta['build_id']}.zip"
    with zipfile.ZipFile(archive,'x',zipfile.ZIP_DEFLATED) as z:
        for rel in files:z.write(dest/rel,dest.name+'/'+rel)
    with zipfile.ZipFile(archive) as z:assert z.testzip() is None
    receipt.update(zip=str(archive),zip_sha256=sha(archive));(out/'release_report.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(receipt,ensure_ascii=False))

if __name__=='__main__':main()
