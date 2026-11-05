"""Frozen report/DB preservation, legacy shared values and isolated EXE export."""
import argparse,copy,hashlib,importlib.util,json,os,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]
import openpyxl
import order_analytics_exports as ex
import funnel_compact_export as compact
import test_order_analytics as fixture
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def require(ok,message):
    if not ok:raise AssertionError(message)
def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);p.add_argument('--baseline',default='d4c09157e1998cc0373ca1f2c9b30ce31ca53df0');p.add_argument('--exe',type=Path);a=p.parse_args()
    out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
    if a.exe:
        subprocess.run([sys.executable,str(ROOT/'tools/verify_analytics_0390.py'),'--fixture-only','--output',str(out/'fixture')],cwd=ROOT,check=True)
        fixture_dir=out/'fixture'
        with (out/'process.txt').open('w',encoding='utf-8') as log:
            run=subprocess.run([str(a.exe.resolve()),'--analytics-smoke',str(fixture_dir)],cwd=ROOT,env={**os.environ,'REPLENISHMENT_DATA_DIR':str(fixture_dir/'settings'),'QT_QPA_PLATFORM':'windows'},stdout=log,stderr=log,timeout=300)
        evidence=json.loads((fixture_dir/'exe-report.json').read_text('utf-8'))
        require(run.returncode==0 and evidence['passed'] and evidence['frozen_exe'],'EXE acceptance failed')
        b=openpyxl.load_workbook(fixture_dir/'actual-exe-report.xlsx')
        try:
            require(b.sheetnames==['门店经营对比','门店分仓对比','订单五段漏斗'],'EXE sheets')
            for ws in b:
                require(ws.max_column==37 and list(next(ws.values))==compact.HEADERS,'EXE columns')
                require(ws.max_row>1 and ws.freeze_panes=='H2','EXE rows/frozen headers')
            evidence['workbook_sheets']=b.sheetnames;evidence['columns_per_sheet']=37
        finally:b.close()
        evidence['exe_sha256']=sha(a.exe);evidence['process_exit']=run.returncode
    else:
        protected=['model.py','allocation.py','one_box_exception.py','order_analytics.py','engine.py','default_config.json','normal_exports.py']
        hashes={n:sha(ROOT/'src'/n) for n in protected}
        for n,h in hashes.items():require(hashlib.sha256(subprocess.check_output(['git','show',a.baseline+':src/'+n],cwd=ROOT)).hexdigest()==h,'Core changed: '+n)
        oldfile=out/'baseline_export.py';oldfile.write_bytes(subprocess.check_output(['git','show',a.baseline+':src/order_analytics_exports.py'],cwd=ROOT))
        spec=importlib.util.spec_from_file_location('prior_export',oldfile);old=importlib.util.module_from_spec(spec);spec.loader.exec_module(old)
        f=fixture.AnalyticsTests();f.setUp()
        try:
            f.f.save('return',[{'门店确认数量':12}]);f.f.save('payment',[{'付款单商品数量':6,'实际金额':19.5}]);f.f.save('receipt',[{'实际收货数量':3}])
            r=f.report();before=copy.deepcopy(r);db=sha(f.path)
            old.export(f.path,r,out/'baseline.xlsx');ex.export(f.path,r,out/'经营分析_37列_合成示例.xlsx')
            b=openpyxl.load_workbook(out/'baseline.xlsx');n=openpyxl.load_workbook(out/'经营分析_37列_合成示例.xlsx');count=0
            try:
                for tab in n.sheetnames:
                    prior=list(b[tab].values);headers=list(prior.pop(0));lookup={(v[0],v[2],v[3],v[6]):v for v in prior}
                    for row in list(n[tab].values)[1:]:
                        v=lookup[(row[0],row[2],row[3],row[6])]
                        require(list(row[:7])==list(v[:7]),'Identity/order/date changed');count+=7
                        for i,s in enumerate(compact.service.STAGES):
                            amount=v[headers.index(compact.service.NAMES[s]+'已计价货值')]
                            require(row[9+3*i]==amount,'Known stage value changed');count+=1
                            if row[7+3*i] is not None:
                                require(row[7+3*i]==v[headers.index(compact.service.NAMES[s]+'款数')],'SKU count changed');count+=1
                        for i,(aa,bb) in enumerate(compact.service.PAIRS):
                            require(row[24+3*i]==v[headers.index(compact.service.NAMES[bb]+'/'+compact.service.NAMES[aa]+'保留率')],'Value ratio changed');count+=1
            finally:b.close();n.close()
            selected=[r['heads'][0]['number']];ex.export(f.path,r,out/'selected.xlsx',level='order',orders=selected)
            b=openpyxl.load_workbook(out/'selected.xlsx')
            try:require(b.sheetnames==['订单五段漏斗'] and {row[3] for row in list(b.active.values)[1:]}==set(selected),'Selected order scope changed')
            finally:b.close()
            require(r==before and sha(f.path)==db,'Frozen facts/database mutated')
            evidence={'passed':True,'baseline':a.baseline,'synthetic':True,'shared_cells_compared':count,'differences':0,'frozen_report_unchanged':True,'database_unchanged':True,'protected_source_sha256':hashes,'columns':compact.HEADERS,'allowed_changes':['37-column projection and reduced tabs','mixed inventory-unit quantity sums explicitly approved','quantity/SKU ratios and head-to-tail ratios','unknown stage SKU count blank instead of 0','header business comments and formats','standalone detail export keeps original columns']}
        finally:f.tearDown()
    (out/'report.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2),'utf-8');print(json.dumps(evidence,ensure_ascii=False))
if __name__=='__main__':main()
