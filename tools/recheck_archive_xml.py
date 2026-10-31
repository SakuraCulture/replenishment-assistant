"""Independent sparse XML comparison including every trailing blank column.

Reads existing QA files only; compares ordered rows, cell storage types, literal
values and resolved number formats. Missing cells mean blank, never numeric 0.
"""
import json,sys,zipfile,xml.etree.ElementTree as ET
from pathlib import Path
from collections import Counter
from openpyxl.styles.numbers import BUILTIN_FORMATS
from openpyxl.utils import column_index_from_string
ROOT=Path(__file__).resolve().parents[1];Q=ROOT/'qa/batch_archive_012/real-exports'
NS='{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
BLANK=('n',None,'General')
def rows(path,sheet):
    with zipfile.ZipFile(path) as z:
        styles=ET.fromstring(z.read('xl/styles.xml'));formats=dict(BUILTIN_FORMATS)
        custom=styles.find(NS+'numFmts')
        if custom is not None:formats.update({int(x.attrib['numFmtId']):x.attrib['formatCode'] for x in custom})
        xfs=[formats[int(x.attrib.get('numFmtId','0'))] for x in styles.find(NS+'cellXfs')]
        if 'xl/sharedStrings.xml' in z.namelist():raise ValueError('Unexpected shared string encoding; extend oracle explicitly')
        with z.open(f'xl/worksheets/sheet{sheet}.xml') as f:
            for _,element in ET.iterparse(f,events=['end']):
                if element.tag!=NS+'row':continue
                result={}
                for c in element:
                    pos=column_index_from_string(''.join(v for v in c.attrib['r'] if v.isalpha()))
                    kind=c.attrib.get('t','n');number=xfs[int(c.attrib.get('s','0'))]
                    if kind=='inlineStr':
                        inline=c.find(NS+'is');value=''.join(t.text or '' for t in inline.iter(NS+'t')) if inline is not None else None
                    else:
                        v=c.find(NS+'v');value=v.text if v is not None else None
                    result[pos]=(kind,value,number)
                element.clear();yield result
def keys(header):
    seen=Counter();result=[]
    for i in range(1,max(header)+1):
        v=header.get(i,BLANK)[1];seen[v]+=1;result.append((v,seen[v]))
    return result
def main():
    heads=json.loads((Q/'heads.json').read_text(encoding='utf-8'));reports=[]
    for folder in Q.glob('归档导出-*'):
        files=list(folder.glob('*.xlsx'));mode='separate' if len(files)==len(heads) else 'combined'
        kind='calculation' if '完整计算表' in files[0].name else 'signed';count=0;rc=0
        for path in sorted(files):
            it=rows(path,1);header=next(it);cols=keys(header)[6:];first=next(it,None)
            if first is None:raise AssertionError('Real fixture unexpectedly empty')
            date,store,_,wh,_,rev=[first.get(i,BLANK)[1] for i in range(1,7)]
            selected=[(i,h) for i,h in enumerate(heads) if h['warehouse']==wh and (mode=='combined' or (h['day']==date and h['store']==store))]
            import itertools
            actual=itertools.chain([first],it)
            for index,h in selected:
                old=rows(Q/f'old-{index}.xlsx',1 if kind=='calculation' else 3);old_cols=keys(next(old));positions={k:i+1 for i,k in enumerate(old_cols)}
                for original in old:
                    row=next(actual,None)
                    if row is None:raise AssertionError('missing row')
                    identity=[h['day'],h['store'],h['name'],h['warehouse'],h['number'],f'V{h["revision"]:03d}']
                    if [row.get(i,BLANK)[1] for i in range(1,7)]!=identity:raise AssertionError('identity/order mismatch')
                    if max(row)>len(cols)+6:raise AssertionError('extra column')
                    for j,k in enumerate(cols,7):
                        wanted=original.get(positions[k],BLANK) if k in positions else BLANK
                        got=row.get(j,BLANK)
                        if got!=wanted:raise AssertionError((path.name,h['number'],rc,k,got,wanted))
                        count+=1
                    rc+=1
            if next(actual,None) is not None:raise AssertionError('extra row')
        reports.append({'kind':kind,'mode':mode,'files':len(files),'rows':rc,'all_business_cells_including_trailing_blanks':count,'differences':0})
    if len(reports)!=4:raise AssertionError('missing export scenarios')
    result={'passed':True,'scenarios':reports,'scope':'all columns including trailing blanks; exact XML literal values, types and resolved number formats; identity and row order; no tolerance or ignored business fields','differences':0}
    (Q/'xml_equivalence.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');print('passed XML',sum(r['all_business_cells_including_trailing_blanks'] for r in reports))
if __name__=='__main__':main()
