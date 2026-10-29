"""Fixed baseline export oracle with only the user-requested exclusion projection."""
import sys,json,copy,importlib.util,hashlib,zipfile,xml.etree.ElementTree as ET,argparse
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];QA=ROOT/'qa/full_workflow_073';sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tools')]
import normal_exports as current_merchant,xlsx_exports as current_xlsx,history_read,result_semantics as sem,normal_orders,work_repository as wr
from structure_oracle import excel_snapshot
from parallel_qa import diff


def baseline(name):
    spec=importlib.util.spec_from_file_location('baseline_'+name,QA/'baseline/src'/f'{name}.py');module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


def excluded(d):return str(d.get('S') or '').strip()=='禁用' or str(d.get('_meta',{}).get('export_status') or '').strip()=='停采'


def expected(result,keys=()):
    blocked={d['E'] for d in result['rows'] if excluded(d)};keys=set(keys)|{(result['store'],code) for code in blocked};rows=[d for d in result['rows'] if not excluded(d)];out={**result,'rows':rows,'summary':sem.summarize(rows),'must_list_audit':[a for a in result['must_list_audit'] if a['sku'] not in blocked]}
    if result.get('import_audit'):out['import_audit']={**result['import_audit'],'conflicts':[c for c in result['import_audit'].get('conflicts',[]) if (c['store'],c['sku']) not in keys]}
    return out


def compare(a,b):
    differences=diff(excel_snapshot(a),excel_snapshot(b));assert not differences,differences[:5]
    with zipfile.ZipFile(a) as x,zipfile.ZipFile(b) as y:
        parts=[n for n in x.namelist() if n.startswith('xl/comments/') or n.startswith('xl/drawings/') or n=='docProps/custom.xml'];assert parts==[n for n in y.namelist() if n.startswith('xl/comments/') or n.startswith('xl/drawings/') or n=='docProps/custom.xml']
        for n in parts:assert x.read(n)==y.read(n),n


def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);p.add_argument('--real',action='store_true');a=p.parse_args();out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
    old_m=baseline('normal_exports');old_x=baseline('xlsx_exports');repo=history_read.repository(ROOT/'qa/archive_072/snapshot.sqlite3') if a.real else normal_orders.Repository(wr.Repository(QA/'normal_backend/settings'))
    records=repo.headers();checks=[];results={};states={}
    for head in records:
        order=repo.get(head['day'],head['store']);r=order['payload']['result'];before=normal_orders.fingerprint_owned(r,order['payload'].get('new_focus'));projected=expected(r)
        calculated=sem.summarize(r['rows']);assert calculated.keys()==r['summary'].keys()
        # Persistence sorts JSON keys; verify cached card values/types by named field.
        assert all(not diff(calculated[k],r['summary'][k]) for k in calculated),head['store']
        baseline_order={**order,'payload':{**order['payload'],'result':projected}};name=head['store'];oldpath=out/(name+'-old-merchant.xlsx');newpath=out/(name+'-new-merchant.xlsx')
        old_m.export(baseline_order,oldpath);current_merchant.export(order,newpath);compare(oldpath,newpath)
        oldpath=out/(name+'-old-internal.xlsx');newpath=out/(name+'-new-internal.xlsx');old_x.export_xlsx(projected,oldpath);current_xlsx.export_xlsx(r,newpath);compare(oldpath,newpath)
        assert normal_orders.fingerprint_owned(r,order['payload'].get('new_focus'))==before
        checks.append({'store':name,'raw_rows':len(r['rows']),'export_rows':len(projected['rows']),'removed':len(r['rows'])-len(projected['rows']),'merchant_cells_types_order_styles_comments':0,'internal_cells_types_order_styles':0,'raw_signed_result_unchanged':True});results[name]=r;states[name]={'name':r['store_name'],'state':'已完成','error':''}
        print(name+' passed',flush=True)
    if not a.real:
        keys={(s,d['E']) for s,r in results.items() for d in r['rows'] if excluded(d)};oldpath=out/'old-batch.xlsx';newpath=out/'new-batch.xlsx'
        old_x.export_batch({s:expected(r,keys) for s,r in results.items()},states,oldpath);current_xlsx.export_batch(results,states,newpath);compare(oldpath,newpath)
        checks.append({'batch_cells_types_order_styles':0})
    report={'passed':True,'differences':0,'baseline_commit':'84a0671bd81daa8c2e94a8648c2332b9fc8fa2a9','expected_delta':'remove existing disabled/stopped rows from working product, priority-match and known excluded conflict detail projections; preserve all remaining values/types/order/styles and raw archive fingerprints','whitelist':'structure_oracle exact clock/build cells only; merchant comments/custom properties also exact','checks':checks}
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(report,ensure_ascii=False))
if __name__=='__main__':main()
