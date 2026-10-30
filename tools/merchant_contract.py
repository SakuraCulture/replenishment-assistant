"""Explicit merchant contract delta against frozen 0.7.0, full real result."""
import os,sys,json,pickle,zlib,hashlib,subprocess,time,copy
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if '--worker' in sys.argv:
    source,out,mode=sys.argv[2:];sys.path.insert(0,str(Path(source)/'src'))
    import normal_exports,new_product_focus as focus
    result=pickle.loads(zlib.decompress((ROOT/'qa/normal_070/rc3-candidate/real_None_True.pklz').read_bytes()))
    order={'day':'2026-10-30','number':'QA-REAL-EXPORT-CONTRACT','revision':1,'current_revision':1,'store':result['store'],'payload':{'result':result,'new_focus':focus.empty('2026-10'),'item_ids':{d['E']:str(i) for i,d in enumerate(result['rows'])}}}
    start=time.perf_counter();normal_exports.export(order,Path(out)/(mode+'.xlsx'));(Path(out)/(mode+'.seconds')).write_text(str(time.perf_counter()-start));sys.exit(0)
out=Path(sys.argv[sys.argv.index('--output')+1]).resolve() if '--output' in sys.argv else ROOT/'qa/export_071/merchant-contract';out.mkdir(parents=True,exist_ok=False)
for mode,source in [('old',ROOT/'qa/export_071/baseline'),('new',ROOT)]:subprocess.run([sys.executable,__file__,'--worker',str(source),str(out),mode],check=True)
sys.path.insert(0,str(ROOT/'src'));import openpyxl,normal_exports
result=pickle.loads(zlib.decompress((ROOT/'qa/normal_070/rc3-candidate/real_None_True.pklz').read_bytes()));by={d['E']:d for d in result['rows']}
a=openpyxl.load_workbook(out/'old.xlsx');b=openpyxl.load_workbook(out/'new.xlsx');assert a.sheetnames==b.sheetnames
counts={};checked=0
for left,right in zip(a,b):
    assert [c.value for c in left[1][:20]]==[c.value for c in right[1][:20]]
    assert [c.value for c in right[1]][20:]==['补货单号','决策理由'] and right.max_column==22
    expected=[r for r in left.iter_rows(min_row=2) if by[r[4].value]['_source']=='资料匹配'];actual=list(right.iter_rows(min_row=2));assert len(expected)==len(actual)
    for x,y in zip(expected,actual):
        d=by[x[4].value];assert y[4].value==x[4].value
        for i in range(20):
            av='未提供' if i==17 and x[i].value=='未提供（不作为数量拦截）' else x[i].value
            assert av==y[i].value and type(av)==type(y[i].value) and x[i].data_type==y[i].data_type,(left.title,i,x[i].value,y[i].value)
            for f in ['font','fill','border','alignment','protection']:assert copy.copy(getattr(x[i],f))==copy.copy(getattr(y[i],f))
            assert y[i].number_format==('0.00' if i==15 else '@' if i==17 else x[i].number_format)
            checked+=1
        assert x[20].value==y[20].value and y[21].value==normal_exports.decision(d) and len(y[21].value)<=6
    counts[left.title]={'before':left.max_row-1,'after':right.max_row-1}
    assert not any(v.hidden for v in right.column_dimensions.values())
a.close();b.close()
r={'passed':True,'version':'0.7.1','source_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),'baseline_commit':'850d31ded8595df72d221c7dc87358d522432ca7','fixture_sha256':hashlib.sha256((ROOT/'qa/normal_070/rc3-candidate/real_None_True.pklz').read_bytes()).hexdigest(),'checked_preserved_cells':checked,'unapproved_differences':0,'counts':counts,'seconds_single_trial':{m:float((out/(m+'.seconds')).read_text()) for m in ['old','new']},'authorized_changes':['exclude non-matched central warehouse rows from all 3 sheets','remove seven audit columns, retain U number, append V concise decision','P numeric two decimal display and width, R text/short missing status and width','U/V visible widths','comments/description now point to archive rather than removed hidden columns'],'limits':'single full real-store snapshot, explicit empty focus; content comparison independent before/after processes; timing only one trial and includes smaller output, not pure style reuse benefit'}
(out/'merchant_report.json').write_text(json.dumps(r,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(r,ensure_ascii=False))
