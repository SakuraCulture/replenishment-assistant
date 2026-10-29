"""Compare the old temporary-XLSX boundary to records, using real saved cells."""
import argparse,json,sys,copy,zipfile,pickle,importlib.util
from pathlib import Path
from datetime import datetime,date,time,timedelta,timezone
from decimal import Decimal
root=Path(__file__).resolve().parent;sys.path[:0]=[str(root/'src'),str(root/'tools')]
import openpyxl,input_adapter as adapter
from openpyxl.cell import WriteOnlyCell
from table_records import bridge_table
from structure_oracle import differences,write_json


def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    spec=importlib.util.spec_from_file_location('frozen_adapter',root/'qa/structure_061/baseline/src/input_adapter.py');old_adapter=importlib.util.module_from_spec(spec);spec.loader.exec_module(old_adapter)
    out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);cases=[]
    def check(name,headers,rows,required=(),header_text=False):
        book=openpyxl.Workbook();ws=book.active;ws.title='原中转表'
        def append(values,force):
            cells=[]
            for v in values:
                c=WriteOnlyCell(ws,v)
                if force and isinstance(v,str):c.data_type='s'
                cells.append(c)
            ws.append(cells)
        def old():
            append(headers,header_text)
            for row in rows:append([row.get(h) for h in headers],True)
            path=out/(name+'.xlsx');book.save(path);book.close()
            return old_adapter.read_table(path,'原中转表',required)
        def outcome(fn):
            try:return {'value':fn()}
            except Exception as ex:return {'error':(type(ex).__name__,str(ex))}
        before=pickle.dumps(rows);x=outcome(old);y=outcome(lambda:bridge_table(headers,rows,'原中转表',required,header_text=header_text))
        diff=differences(x,y);cases.append({'case':name,'differences':len(diff),'details':diff})
        assert before==pickle.dumps(rows),'source records changed'
    values=[None,'',0,0.0,False,True,1,1.0,-7,1e-20,1.2345678901234567,1234567890123456789012345,
            '0000123','1234567890123456789012345',' 000123 ','=SKU','#N/A','a\r\nb\t中','x'*32770,'_x000D_','a\r尾','😀',
            Decimal('1.234567890123456789'),date(2026,10,29),datetime(2026,10,29,12,34,56,123456),
            datetime(1900,1,1),datetime(1899,12,31),datetime(1904,1,1),time(12,34,56,123456),timedelta(days=1,seconds=2,microseconds=345678),
            float('inf'),float('nan')]
    for i,v in enumerate(values):
        check('cell_'+str(i),['id','value'],[{'id':str(i),'value':v}],['id'])
    check('blanks',['id','value'],[{}, {'id':None,'value':''},{'id':'last','value':0}])
    check('header_spaces',[' id ','other',None],[{' id ':'0001','other':False}],['id'])
    check('duplicate_unused',['id','unused','unused'],[{'id':'a','unused':7}],['id'])
    check('duplicate_required',['id','id'],[{'id':'a'}],['id'])
    check('missing',['a'],[{'a':1}],['id'])
    check('header_formula',['id','=HEADER',None,''],[{'id':'a','=HEADER':7}],['id'])
    check('header_forced_text',['id','=HEADER'],[{'id':'a','=HEADER':7}],['id'],True)
    check('illegal_character',['id'],[{'id':'bad\x01'}])
    check('timezone',['id'],[{'id':datetime(2026,10,29,tzinfo=timezone.utc)}])
    # A real cached formula and a missing cache still enter through data_only Excel IO.
    path=out/'cached.xlsx';book=openpyxl.Workbook();ws=book.active;ws.append(['id','cached','empty']);ws.append(['000123','=1+1','=2+2']);book.save(path);book.close()
    with zipfile.ZipFile(path) as z:parts={n:z.read(n) for n in z.namelist()}
    import xml.etree.ElementTree as ET
    key='xl/worksheets/sheet1.xml';tree=ET.fromstring(parts[key]);ns={'s':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    cell=next(c for c in tree.findall('.//s:c',ns) if c.get('r')=='B2');cell.find('s:v',ns).text='2';parts[key]=ET.tostring(tree,encoding='utf-8')
    with zipfile.ZipFile(path,'w') as z:
        for n,b in parts.items():z.writestr(n,b)
    rows,info=adapter.read_table(path,None,['id']);assert rows[0]['cached']==2 and rows[0]['empty'] is None
    check('formula_cache',info['headers'],rows,['id'])
    result={'passed':not any(c['differences'] for c in cases),'cases':cases,'difference_count':sum(c['differences'] for c in cases),'comparison':'strict type and value; errors include class and exact message; physical blank row offsets retained'}
    write_json(out/'memory_report.json',result);print(json.dumps({'passed':result['passed'],'cases':len(cases),'differences':result['difference_count']}));assert result['passed']

if __name__=='__main__':main()
