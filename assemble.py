"""Seal a distinct verified build; never consume historical QA implicitly."""
import argparse,hashlib,json,shutil,subprocess,zipfile
from pathlib import Path
root=Path(__file__).resolve().parent
parser=argparse.ArgumentParser()
parser.add_argument('--delivery',type=Path,required=True)
parser.add_argument('--budget-report',type=Path,required=True)
parser.add_argument('--batch-report',type=Path,required=True)
parser.add_argument('--ui-report',type=Path,required=True)
parser.add_argument('--warehouse-report',type=Path,required=True)
parser.add_argument('--warehouse-ui-report',type=Path,required=True)
parser.add_argument('--workbench-report',type=Path,required=True)
parser.add_argument('--work-ui-report',type=Path,required=True)
parser.add_argument('--structure-report',type=Path)
parser.add_argument('--memory-lxml-report',type=Path)
parser.add_argument('--memory-etree-report',type=Path)
for flag in ['parallel-report','parallel-forced-report','performance-report','intake-ui-report','intake-controls-report']:parser.add_argument('--'+flag,type=Path)
for flag in ['normal-report','normal-ui-report','normal-equivalence-report']:parser.add_argument('--'+flag,type=Path)
for flag in ['export-report','delivery-report','export-ui-report','merchant-report']:parser.add_argument('--'+flag,type=Path)
args=parser.parse_args();dest=args.delivery.resolve()
digest=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
meta=json.loads((dest/'版本信息.json').read_text(encoding='utf-8'))
commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip()
assert commit==meta['source_commit'],'Commit differs from packaged source'
assert not subprocess.check_output(['git','status','--porcelain'],cwd=root,text=True).strip(),'Commit pending source changes first'
evidence={}
extra=[('parallel',args.parallel_report),('parallel_forced',args.parallel_forced_report),('performance',args.performance_report),('intake_ui',args.intake_ui_report),('intake_controls',args.intake_controls_report)] if meta['version']=='0.6.2' else [('structure',args.structure_report),('memory_lxml',args.memory_lxml_report),('memory_etree',args.memory_etree_report)]
if meta['version']=='0.7.0':extra=[('normal',args.normal_report),('normal_ui',args.normal_ui_report),('normal_equivalence',args.normal_equivalence_report),('intake_ui',args.intake_ui_report)]
if meta['version']=='0.7.1':extra=[('normal',args.normal_report),('normal_ui',args.normal_ui_report),('normal_equivalence',args.normal_equivalence_report),('intake_ui',args.intake_ui_report),('export',args.export_report),('delivery',args.delivery_report),('export_ui',args.export_ui_report),('merchant',args.merchant_report)]
assert all(path is not None for name,path in extra),'Required version-specific acceptance reports missing'
for name,path in [('budget',args.budget_report),('batch',args.batch_report),('packaged_ui',args.ui_report),('warehouse',args.warehouse_report),('warehouse_ui',args.warehouse_ui_report),('workbench',args.workbench_report),('work_ui',args.work_ui_report)]+extra:
    data=json.loads(path.read_text(encoding='utf-8'))
    assert data['passed'] and data['version']==meta['version'] and data['source_commit']==commit,(name,'stale or failed evidence')
    assert not data.get('development'),(name,'development evidence cannot seal release')
    if name in {'packaged_ui','warehouse_ui','work_ui','intake_ui','normal_ui','export_ui'}:assert data['exe_sha256']==digest(dest/meta['entry'])
    if name=='merchant':
        assert data['baseline_commit']=='850d31ded8595df72d221c7dc87358d522432ca7' and data['unapproved_differences']==0
    if name=='export':
        assert data['baseline_commit']=='850d31ded8595df72d221c7dc87358d522432ca7'
        assert data['differences']==0 and not data['changed_protected']
        assert all(x['before']==x['after'] for x in data['core'].values())
        assert data['candidate_sources']=={str(p.relative_to(root)):digest(p) for p in sorted((root/'src').glob('*')) if p.is_file()}
        assert all(data['summary'][c][m]['n']>=3 for c in ['subset','five'] for m in ['old','new'])
    if name=='normal_equivalence':
        assert data['baseline_commit']==('850d31ded8595df72d221c7dc87358d522432ca7' if meta['version']=='0.7.1' else 'a4176229182079bd4d886fa44e54798f6c218cee')
        assert data['differences']==0 and data['strict_order'] and not data['changed_protected']
        assert all(x['before']==x['after'] for x in data['core'].values())
        assert data['candidate_sources']=={str(p.relative_to(root)):digest(p) for p in sorted((root/'src').glob('*')) if p.is_file()}
    if name in {'parallel','parallel_forced'}:
        assert data['baseline_commit']=='ebaf8d059667968d11ee994c20837b4313f32337'
        assert data['differences']==0 and data['strict_order'] and not data['changed_protected']
        assert data['candidate_sources']=={str(p.relative_to(root)):digest(p) for p in sorted((root/'src').glob('*')) if p.is_file()}
    if name=='performance':
        assert data['rounds']>=3 and set(data['summary'])=={'single','two','six','mixed'}
        assert data['candidate_sources']=={str(p.relative_to(root)):digest(p) for p in sorted((root/'src').glob('*')) if p.is_file()}
    if name=='structure':
        assert data['baseline_commit']=='e50072cc00c6997aed9fe1113012192210ad7eb3'
        assert data['difference_count']==0 and not data['changed_preserved_files'] and not data['source_changed_since_capture']
        current={str(p.relative_to(root)):digest(p) for p in sorted((root/'src').glob('*')) if p.is_file()}
        assert data['candidate_sources']==current,'source differs from equivalence capture'
    if name.startswith('memory_'):assert data['difference_count']==0 and data['lxml']==(name=='memory_lxml')
    evidence[name]=data
assert not (dest/'_internal/icuuc.dll').exists()
out=root/'releases'/meta['version']/meta['build_id'];out.mkdir(parents=True,exist_ok=False)
notes={'version':meta['version'],'source_commit':commit,'build_id':meta['build_id'],'verified':['business budget regression','single/multi-file grouping and conflicts','serial vs process concurrency equality','budget isolation and failures','packaged EXE interactions and exports','warehouse snapshots, per-store supply routing, zero-stock secondary demand retained, exact assigned-master price/carton/weights'],'not_verified':['other computers','byte-reproducible rebuild','two actual warehouse feeds; routing scenarios use synthetic fixtures'],'rules':'Original 0.3 model unchanged; routing precedes calculation, without stock reservation or quantity cap; no daily-rate unification or experimental protection period'}
notes['verified']+=['daily atomic saves with explicit overwrite confirmation and stale revision rejection','complete parameter presets and store defaults with actual per-store weight rebinding','snapshot restore without original Excel and packaged workbench interactions']
if meta['version'] in {'0.7.0','0.7.1'}:
    notes['verified']+=['fixed prior-release baseline exact ordered full-field/Excel equivalence without new selection','normal-order unique identity, CAS revisions, complete rollback, stable row IDs, frozen numbered merchant exports','new-product eligibility and explicit shared-budget allocation including unlimited/zero budgets','actual EXE home/work/history, preview cancel/apply, confirmation cancel/overwrite, restore and export','two-process intake EXE regression']
    notes['not_verified']+=['external historical confirmation/order/receipt import and linking are deferred','merchant return-file processing is not implemented']
    if meta['version']=='0.7.1':
        notes['verified']+=['fixed 0.7.0 internal workbook ordered values/types/styles equivalence and alternating export performance','shared immutable styles, measured row progress, safe cancellation/retry','four-slot calculation queue starts fifth before slower stores finish','merchant store picker with all/subset selection, one background batch signing transaction, sequential per-store export','authorized merchant-only changes: central-warehouse rows, 22 columns, concise decision reason, P price/R status display; archive remains complete']
        notes['not_verified']+=['performance of the user exact five-store batch; five-store timing is labelled synthetic','parallel export and reduced internal summary deliberately not implemented']
else:notes['verified']+=(['fixed 0.6.1 exact ordered field/type and Excel equivalence, including forced out-of-order completion','bounded two-process file checks, cancel/close/failure/retry with no remaining children','alternating serial/parallel time and process-tree sampled memory measurements'] if meta['version']=='0.6.2' else ['fixed 0.6.0 exact keyed field/type and Excel equivalence','temporary XLSX vs records boundary in both XML environments'])
notes['verified']+=['unchanged model/exception/allocation/default bytes']
(dest/'验收记录.json').write_text(json.dumps(notes,ensure_ascii=False,indent=2),encoding='utf-8')
files=sorted(p for p in dest.rglob('*') if p.is_file());manifest={str(p.relative_to(dest)):digest(p) for p in files}
archive=dest.parent/f"补货助手_{meta['version']}_{meta['build_id']}.zip"
with zipfile.ZipFile(archive,'x',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as z:
    for p in files:z.write(p,p.relative_to(dest.parent))
with zipfile.ZipFile(archive) as z:assert z.testzip() is None
for name,data in evidence.items():(out/(name+'_report.json')).write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
for name in ['版本信息.json','验收记录.json','使用说明.txt']:shutil.copy2(dest/name,out/name)
(out/'delivery_sha256.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
report={**meta,'archive':str(archive),'zip_sha256':digest(archive),'exe_sha256':digest(dest/meta['entry']),'file_count':len(files),'zip_bytes':archive.stat().st_size}
(out/'release_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(report,ensure_ascii=False,indent=2))
