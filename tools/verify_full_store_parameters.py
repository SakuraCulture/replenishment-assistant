"""Strict 0.25 -> 0.26 comparison; only exact schema version metadata may differ."""
import argparse,copy,json,re
from pathlib import Path
from xml.etree import ElementTree as ET
import parallel_qa as qa

def compare(folder):
    before,after=folder/'before',folder/'after';allowed=[];cases=[]
    names=json.loads((before/'capture.json').read_text(encoding='utf-8'))['cases']
    if names!=json.loads((after/'capture.json').read_text(encoding='utf-8'))['cases']:raise AssertionError('case order changed')
    def normalize(a,b,path,case):
        if isinstance(a,dict) and isinstance(b,dict):
            if path.endswith('.version') and a.get('parameter_schema_version')==1 and b.get('parameter_schema_version')==2:
                allowed.append({'case':case,'path':path+'.parameter_schema_version','before':1,'after':2});b['parameter_schema_version']=1
            for k in a:
                if k in b:normalize(a[k],b[k],path+'.'+str(k),case)
        elif isinstance(a,(list,tuple)) and type(a)==type(b):
            for i,(x,y) in enumerate(zip(a,b)):normalize(x,y,path+f'[{i}]',case)
    for name in names:
        a,b=qa.clean(qa.read(before/(name+'.pklz'))),qa.clean(qa.read(after/(name+'.pklz')))
        normalize(a,b,'$',name);cases.append({'case':name,'differences':qa.diff(a,b)})
    def text(xml):return ''.join(ET.fromstring(xml).itertext())
    for file in before.glob('*.xlsx'):
        a,b=qa.excel_snapshot(file),qa.excel_snapshot(after/file.name)
        for i,(old,new) in enumerate(zip(a['sheets'],b['sheets'])):
            for address,ov in old['cells'].items():
                nv=new['cells'].get(address)
                if ov==nv or nv is None:continue
                ot,nt=ET.fromstring(ov),ET.fromstring(nv)
                on=ot.find('normalized-dynamic-value');nn=nt.find('normalized-dynamic-value')
                if on is not None and nn is not None:
                    try:x,y=json.loads(on.text),json.loads(nn.text)
                    except (ValueError,TypeError):continue
                    if isinstance(x,dict) and x.get('parameter_schema_version')==1 and y.get('parameter_schema_version')==2:
                        y['parameter_schema_version']=1
                        if x==y:nn.text=on.text
                elif old['identity']['name']==new['identity']['name']=='版本及参数' and address.startswith('B') and text(old['cells'].get('A'+address[1:],'<empty/>')).endswith(' / parameter_schema_version') and old['cells'].get('A'+address[1:])==new['cells'].get('A'+address[1:]) and text(ov)=='1' and text(nv)=='2':
                    for node in nt.iter():
                        if node.text=='2':node.text='1'
                if ET.tostring(ot)==ET.tostring(nt):
                    new['cells'][address]=ov;allowed.append({'case':file.name,'path':f'sheets[{i}].cells.{address}','before':'schema 1','after':'schema 2'})
        cases.append({'case':file.name,'differences':qa.diff(a,b)})
    core={n:{'before':h,'after':qa.sha(qa.ROOT/'src'/n)} for n,h in json.loads((folder/'baseline.json').read_text(encoding='utf-8'))['core'].items()}
    report={'baseline_commit':'2721aa34aec0691e83f713844a1d601f6f59a572','strict_values_types_order':True,'existing_dynamic_whitelist':qa.WHITELIST,'allowed_schema_metadata_changes':allowed,'cases':cases,'core':core,'unexpected_differences':sum(len(c['differences']) for c in cases)}
    report['passed']=not report['unexpected_differences'] and all(v['before']==v['after'] for v in core.values())
    (folder/'equivalence_final.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'passed':report['passed'],'cases':len(cases),'schema_metadata_changes':len(allowed),'unexpected_differences':report['unexpected_differences']}))
    if not report['passed']:raise AssertionError('see equivalence_final.json')

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--qa',type=Path,required=True);compare(p.parse_args().qa)
