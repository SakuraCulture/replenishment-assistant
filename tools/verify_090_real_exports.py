"""Exact frozen five-store merchant export comparison; reason differences individually recorded."""
import argparse,hashlib,json,subprocess,sys,zipfile,xml.etree.ElementTree as ET
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def child(source,snapshot,out):
    sys.path.insert(0,str(source/'src'))
    from history_read import repository
    import normal_exports as ne,work_repository as wr
    repo=repository(snapshot);report=[]
    for h in repo.headers():
        order=repo.get(h['day'],h['store'],h['revision']);path=out/ne.filename(order);ne.export(order,path)
        r=order['payload']['result'];report.append({'store':h['store'],'revision':h['revision'],'file':path.name,'rows':len(r['rows']),'fingerprint':order['digest'],'canonical':hashlib.sha256(wr.dumps(order).encode()).hexdigest(),'exported':{sheet:[{'sku':d['E'],'reason':ne.decision(d)} for d in rows] for sheet,rows in ne.groups(r,order['payload'].get('new_focus')).items()}})
    (out/'capture.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
def main():
    p=argparse.ArgumentParser();p.add_argument('--source',type=Path);p.add_argument('--baseline',type=Path);p.add_argument('--snapshot',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
    if a.source:return child(a.source.resolve(),a.snapshot.resolve(),out)
    sys.path.insert(0,str(ROOT/'tools'));from structure_oracle import excel_snapshot;from parallel_qa import diff
    before=hashlib.sha256(a.snapshot.read_bytes()).hexdigest()
    for name,source in [('old',a.baseline),('new',ROOT)]:subprocess.run([sys.executable,__file__,'--source',str(source.resolve()),'--snapshot',str(a.snapshot.resolve()),'--output',str(out/name)],check=True)
    old=json.loads((out/'old/capture.json').read_text(encoding='utf-8'));new=json.loads((out/'new/capture.json').read_text(encoding='utf-8'));differences=[];allowed=[]
    for x,y in zip(old,new):
        for sheet,rows in x['exported'].items():
            for i,(r,s) in enumerate(zip(rows,y['exported'][sheet]),2):
                assert r['sku']==s['sku']
                if r['reason']!=s['reason']:allowed.append({'store':x['store'],'sku':r['sku'],'sheet':sheet,'cell':f'V{i}','before':r['reason'],'after':s['reason']});s['reason']=r['reason']
        first,second=excel_snapshot(out/'old'/x['file']),excel_snapshot(out/'new'/x['file'])
        for u,v in zip(first['sheets'],second['sheets']):
            for change in [z for z in allowed if z['store']==x['store'] and z['sheet']==u['identity']['name']]:
                cell=change['cell'];a1,b1=ET.fromstring(u['cells'][cell]),ET.fromstring(v['cells'][cell]);ta,tb=list(a1.iter())[-1],list(b1.iter())[-1];assert ta.text==change['before'] and tb.text==change['after'];ta.text=tb.text='<reason>';assert ET.tostring(a1)==ET.tostring(b1);v['cells'][cell]=u['cells'][cell]
        differences+=diff(first,second,x['store'])
        with zipfile.ZipFile(out/'old'/x['file']) as a1,zipfile.ZipFile(out/'new'/x['file']) as b1:
            for name in a1.namelist():
                if name.startswith(('xl/comments/','xl/drawings/')) or name=='docProps/custom.xml':assert a1.read(name)==b1.read(name)
    differences+=diff(old,new);assert before==hashlib.sha256(a.snapshot.read_bytes()).hexdigest()
    report={'passed':not differences,'rows':sum(x['rows'] for x in old),'stores':len(old),'snapshot_sha256':before,'unexpected_differences':differences,'allowed_reason_changes':allowed,'business_payload_differences':0,'scope':'exact immutable order hash, ordered SKU rows, sheets/columns/cell values/types/styles/XML structures/comments/custom properties; only listed V-cell text changes'};(out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8',newline='\n');print(json.dumps({'passed':report['passed'],'rows':report['rows'],'reason_changes':len(allowed)}));assert report['passed']
if __name__=='__main__':main()
