"""Ten-store merchant workbooks: only approved moving-goods removals allowed."""
import json,pickle,zlib,sys,time
from pathlib import Path
import openpyxl
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'));import normal_exports
q=ROOT/'qa/inbound_091';trial=ROOT.parent/'inbound_floor_trial_20261030';out=q/'real_exports';out.mkdir(exist_ok=False)
expected=json.loads((trial/'result.json').read_text(encoding='utf-8'))['changes'];frozen=json.loads((trial/'frozen.json').read_text(encoding='utf-8'));records=[]
def read(p):return pickle.loads(zlib.decompress(p.read_bytes()))
def cells(ws):return [[(c.value,c.data_type,c.number_format,str(c._style)) for c in row] for row in ws]
for entry in frozen['orders']:
    a=read(trial/'frozen'/entry['file']);b=read(q/'real'/entry['file']);old=out/(entry['store']+'-old.xlsx');new=out/(entry['store']+'-new.xlsx');normal_exports.export(a,old);normal_exports.export(b,new)
    before=openpyxl.load_workbook(old);after=openpyxl.load_workbook(new);assert before.sheetnames==after.sheetnames
    removed={d['sku'] for d in expected if d['store']==entry['store']};actual=[]
    for x,y in zip(before,after):
        xc,yc=cells(x),cells(y);assert xc[0]==yc[0]
        allowed=[row for row in xc[1:] if str(row[4][0]) not in removed]
        # Removed goods are ordinary moving demand, never the all-new candidates.
        deleted=[row for row in xc[1:] if str(row[4][0]) in removed]
        actual.extend(str(row[4][0]) for row in deleted)
        assert yc[1:]==allowed,(entry['store'],x.title)
        assert str(x.freeze_panes)==str(y.freeze_panes)
        assert [(k,v.width,v.hidden) for k,v in x.column_dimensions.items()]==[(k,v.width,v.hidden) for k,v in y.column_dimensions.items()]
    assert set(actual)==removed and len(actual)==len(removed)
    records.append({'store':entry['store'],'removed':len(actual),'remaining_cell_values_types_styles_order_differences':0});before.close();after.close()
report={'passed':True,'stores':records,'workbooks':20,'removed_rows':sum(x['removed'] for x in records),'remaining_differences':0,'scope':'QA counterfactual files only, no new signed orders or user exports'}
(out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(report,ensure_ascii=False))
