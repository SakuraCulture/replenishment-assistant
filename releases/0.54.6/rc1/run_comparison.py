"""Serial accepted-baseline/candidate comparisons on marked synthetic inputs."""
import hashlib,json,os,statistics,subprocess,sys,time,traceback,zipfile
from pathlib import Path
from xml.etree import ElementTree as ET
Q=Path(__file__).resolve().parent;ROOT=Path.cwd()
def read(path):return json.loads(Path(path).read_text('utf-8'))
def write(path,value):Path(path).write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str)+'\n','utf-8')
def run(name,source,script,arguments):
 output=Q/name;env=os.environ.copy();env.update(PYTHONUTF8='1',QT_QPA_PLATFORM='offscreen')
 env.pop('PYTHONHOME',None);env.pop('PYTHONPATH',None)
 for key,part in [('LOCALAPPDATA','local'),('APPDATA','roaming'),('TEMP','temp'),('TMP','temp')]:
  p=Q/'measurement-environment'/name/part;p.mkdir(parents=True,exist_ok=True);env[key]=str(p)
 args=[sys.executable,str(Q/script),'--source',str(source),'--output',str(output),*arguments]
 with (Q/(name+'.log')).open('wb') as log:
  process=subprocess.Popen(args,cwd=ROOT,env=env,stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW)
  write(Q/'comparison-status.json',{'stage':name,'pid':process.pid,'args':args});code=process.wait()
 result=read(output/'result.json') if (output/'result.json').exists() else {'passed':False,'error':'No worker receipt'}
 if code or not result['passed']:raise RuntimeError(name+' failed: '+result.get('error',''))
 print(json.dumps({'complete':name},ensure_ascii=False),flush=True);return result
def parts(path):
 result={}
 with zipfile.ZipFile(path) as z:
  assert z.testzip() is None
  for name in z.namelist():
   if name=='docProps/core.xml':
    root=ET.fromstring(z.read(name))
    for child in list(root):
     if child.tag.rsplit('}',1)[-1] in ('created','modified'):root.remove(child)
    result[name]=hashlib.sha256(ET.tostring(root)).hexdigest()
   else:
    with z.open(name) as f:result[name]=hashlib.file_digest(f,'sha256').hexdigest()
 return result
def main():
 try:
  assert read(Q/'regression-final.json')['passed'],'Regression did not pass'
  initial=read(Q/'initial-state.json');sources={'old':Path(initial['baseline']),'new':Q/'candidate-01'}
  run('flow-pilot-01',sources['new'],'flow_worker.py',['--stores','2','--rows','100'])
  runs={};reference=None;file_reference={};comparisons=[]
  for trial,variants in ((1,('old','new')),(2,('new','old'))):
   for kind in variants:
    name=f'flow-{trial}-{kind}';r=run(name,sources[kind],'flow_worker.py',['--stores','4','--rows','2500'])
    assert len(r['stages'])==38 and r['default_workers']==2 and len(r['final_statuses'])==8
    assert all(h['next'] is None and all(h['health'].values()) for h in r['final_statuses'])
    if reference is None:reference=r['business_facts']
    assert r['business_facts']==reference,('Business equivalence',name)
    for filename in ('订单经营分析.xlsx','订单经营细则分析.xlsx'):
     digest=parts(Q/name/filename)
     if filename not in file_reference:file_reference[filename]=digest
     assert digest==file_reference[filename],('Workbook content/formula/cache/style/layout difference',name,filename)
     comparisons.append({'run':name,'file':filename,'passed':True,'parts':digest,'ignored':'Core created/modified metadata timestamps only'})
    runs[name]=r;write(Q/'flow-comparison-partial.json',runs);write(Q/'workbook-comparisons.json',comparisons)
  targets={}
  for mode in ('lifecycle','template'):
   pair={}
   for kind in ('old','new'):
    name=f'{mode}-20k-{kind}';pair[kind]=run(name,sources[kind],'target_worker.py',['--kind',mode,'--rows','20000'])
   if mode=='lifecycle':
    assert pair['old']['facts_sha256']==pair['new']['facts_sha256']
    assert pair['old']['receipt']==pair['new']['receipt'] and pair['new']['payload_rows']==20000
    assert pair['new']['data_version_calls']<=2 and pair['old']['data_version_calls']>=40000
   else:
    a,b=parts(pair['old']['file']),parts(pair['new']['file']);assert a==b,'20k template cells/formulas/caches/styles/layout changed'
    comparisons.append({'run':'template-20k','passed':True,'parts':a,'ignored':'Core created/modified metadata timestamps only'})
    write(Q/'workbook-comparisons.json',comparisons)
   targets[mode]=pair;write(Q/'target-comparison.json',targets)
  medians={}
  for p in runs['flow-1-old']['stages']:
   key=p['phase'];item={}
   for kind in ('old','new'):
    values=[next(v for v in r['stages'] if v['phase']==key) for name,r in runs.items() if name.endswith(kind)]
    item[kind]={k:statistics.median(v[k] for v in values) for k in values[0] if k!='phase'}
    item[kind]['seconds_samples']=[v['seconds'] for v in values]
   item['faster_percent']=(item['old']['seconds']-item['new']['seconds'])/item['old']['seconds']*100;medians[key]=item
  totals={kind:statistics.median(sum(v['seconds'] for v in r['stages']) for name,r in runs.items() if name.endswith(kind)) for kind in ('old','new')}
  result={'passed':True,'baseline':'0.54.5 rc1','candidate':'0.54.6 candidate-01','default_workers':2,'repeats':2,
          'flow_shape':{'stores':4,'skus_per_store':2500,'warehouse_orders':8,'sku_warehouse_rows':10000,'operations':38},
          'totals_of_tested_operations':totals,'total_faster_percent':(totals['old']-totals['new'])/totals['old']*100,
          'phases':medians,'targets':targets,'business_equivalence':True,'workbook_equivalence':True,
          'notes':['38-operation sum includes alternative entries and repeated operations; not a single real-user workflow wait.',
                   'Two alternating local repeats, one 20k pair per target. No real platform or business-PC original data.',
                   'Private commit/RSS are sampled; process IO includes cache and does not equal physical disk traffic.',
                   'Input business clocks and reporting clock fixed identically; native/real business clocks are not modified.']}
  write(Q/'comparison.json',result);write(Q/'comparison-status.json',{'stage':'complete','passed':True})
 except BaseException:
  write(Q/'comparison-status.json',{'stage':'failed','error':traceback.format_exc()});raise
if __name__=='__main__':main()
