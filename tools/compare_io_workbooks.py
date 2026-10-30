"""Compare complete merchant XLSX parts, excluding only core document clocks."""
import argparse,json,zipfile,xml.etree.ElementTree as ET
from pathlib import Path


def main():
    p=argparse.ArgumentParser();p.add_argument('--old',type=Path,required=True);p.add_argument('--new',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    files=[f.name for f in a.old.glob('*.xlsx')];assert files==[f.name for f in a.new.glob('*.xlsx')];checks=[];differences=[]
    for name in files:
        with zipfile.ZipFile(a.old/name) as left,zipfile.ZipFile(a.new/name) as right:
            assert left.namelist()==right.namelist();cells=0
            for part in left.namelist():
                x,y=left.read(part),right.read(part)
                if part.startswith('xl/worksheets/sheet'):cells+=x.count(b'<c ')
                if part=='docProps/core.xml':
                    def normalize(raw):
                        root=ET.fromstring(raw)
                        for tag in ('created','modified'):
                            node=root.find('{http://purl.org/dc/terms/}'+tag)
                            if node is not None:node.text='<clock>'
                        return ET.tostring(root)
                    x,y=normalize(x),normalize(y)
                if x!=y:differences.append({'file':name,'part':part})
            checks.append({'file':name,'parts':len(left.namelist()),'cells':cells})
    report={'passed':not differences,'checks':checks,'differences':differences,'scope':'all XLSX ZIP part names/order and XML/binary content; styles, values, types, worksheet/row/column order, comments, workbook properties and filenames','whitelist':['docProps/core.xml:dcterms:created text','docProps/core.xml:dcterms:modified text'],'zip_metadata':'container timestamps/compression metadata not compared; every decompressed part compared'}
    assert not a.output.exists();a.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(report,ensure_ascii=False));assert report['passed']
if __name__=='__main__':main()
