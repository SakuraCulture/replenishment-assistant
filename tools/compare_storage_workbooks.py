"""Strict OOXML comparison: only core created/modified and ZIP metadata excluded."""
import argparse,json,zipfile,xml.etree.ElementTree as ET
from pathlib import Path

def canonical(name,data):
    if name=='docProps/core.xml':
        tree=ET.fromstring(data)
        for child in list(tree):
            if child.tag in ('{http://purl.org/dc/terms/}created','{http://purl.org/dc/terms/}modified'):tree.remove(child)
        return ET.tostring(tree)
    return data

def main():
    p=argparse.ArgumentParser();p.add_argument('--performance',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    reference=a.performance/'old-0/merchant';checks=[]
    for candidate in sorted(a.performance.glob('*/merchant')):
        if candidate==reference:continue
        assert [x.name for x in reference.glob('*.xlsx')]==[x.name for x in candidate.glob('*.xlsx')]
        for old in reference.glob('*.xlsx'):
            with zipfile.ZipFile(old) as x,zipfile.ZipFile(candidate/old.name) as y:
                assert x.namelist()==y.namelist();differences=[];cells=0
                for name in x.namelist():
                    before,after=x.read(name),y.read(name)
                    if canonical(name,before)!=canonical(name,after):differences.append(name)
                    if name.startswith('xl/worksheets/sheet') and name.endswith('.xml'):cells+=len(ET.fromstring(before).findall('.//{http://schemas.openxmlformats.org/spreadsheetml/2006/main}c'))
                checks.append({'candidate':candidate.parent.name,'file':old.name,'cells':cells,'differences':differences})
    report={'passed':all(not r['differences'] for r in checks),'checks':checks,'scope':'all OOXML entries, entry order, workbook structure, values/types/format/comments/custom properties; no sorting of cells or records','whitelist':['docProps/core.xml dcterms:created','docProps/core.xml dcterms:modified','ZIP container metadata']}
    with a.output.open('x',encoding='utf-8') as f:json.dump(report,f,ensure_ascii=False,indent=2)
    assert report['passed'];print(len(checks),'workbooks exact')

if __name__=='__main__':main()
