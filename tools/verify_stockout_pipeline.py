"""Offline alternating host-scheduling comparison. New isolated outputs only.

Uses identical XLSX originals and the actual old/new acquisition, manifest,
projection and publication paths. Platform wait times are explicitly simulated.
Never connects to AX/JST or uses ordinary application settings.
"""
import argparse,copy,hashlib,importlib.util,json,os,shutil,statistics,subprocess,sys,threading,time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT),str(ROOT/'qa/export_071/qa-deps')]


def write(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n','utf-8')


class OfflinePlatform:
    def __init__(self,folder,inputs,codes):
        self.folder,self.inputs,self.codes=folder,inputs,codes
        self.commands={};self.due={};self.results={};self.events=[]

    def rpc(self,service,**payload):
        from aoxiang_jobs import fingerprint
        from stockout_common import file_hash
        if service=='release_ax':return True
        if service=='ax_root':return str(self.folder)
        if service!='ax':raise AssertionError('Real platform access forbidden: '+service)
        action=payload['action'];v=payload['values']
        if action=='configuration':return {'configured':True,'enabled':True,'account_binding':{'account_id':'offline','merchant_code':'QA'},
            'store_mapping_version':'fixture-v1','targets':[{'buyerWarehouseCode':c,'displayName':'测试'+c} for c in self.codes]}
        if action=='reserve':
            command=v['command'];cid=command['command_id']
            if cid not in self.commands:
                folder=self.folder/'command-files'/cid;folder.mkdir(parents=True);items=[]
                for index,code in enumerate(command['scope']['warehouse_codes']):
                    time.sleep(.04)  # Serialized browser query/submission; no concurrent page access.
                    stamp=time.monotonic();self.events.append({'action':'submit','store':code,'at':stamp})
                    self.due[(cid,code)]=stamp+(1.0 if code==self.codes[0] else .6)
                    original=self.inputs/(code+'.xlsx');sha=file_hash(original)
                    target=folder/(str(index)+'-'+sha+'.xlsx');shutil.copyfile(original,target)
                    native=cid+'-'+code
                    self.results[(cid,code)]={'warehouse_code':code,'run_id':native,'file':str(target),'sha256':sha,
                        'bytes':target.stat().st_size,'rows':3,'account_binding':command['account_binding'],'collected_at':'2026-11-07T08:00:00+08:00'}
                    items.append({'index':index,'warehouse_code':code,'run_id':native,'task_id':'accepted-'+native,'state':'RUNNING','result':None})
                self.commands[cid]={'id':cid,'request':command,'fingerprint':fingerprint(command),'sequence':1,'state':'RUNNING','items':items}
            return {'record':copy.deepcopy(self.commands[cid])}
        if action=='status':
            cid=v['command_id'];record=self.commands[cid];changed=False
            for item in record['items']:
                code=item['warehouse_code']
                if item['state']!='SUCCEEDED' and time.monotonic()>=self.due[(cid,code)]:
                    item.update(state='SUCCEEDED',result=self.results[(cid,code)])
                    self.events.append({'action':'collect','store':code,'at':time.monotonic()});changed=True
            if changed:
                record['sequence']+=1
                if all(i['state']=='SUCCEEDED' for i in record['items']):record['state']='SUCCEEDED'
            return copy.deepcopy(record)
        raise AssertionError('Unexpected offline action: '+action)


def sample(args):
    import psutil
    from stockout_repository import Repository
    from stockout_facts import load_catalog
    from stockout_common import file_hash
    from stockout_metrics import query
    folder=args.output.resolve();folder.mkdir();repo=Repository(folder/'settings');repo.initialize()
    codes=['S5','S1','S3','S2','S4'][:args.count]
    repo.save_settings({'stores':[{'code':c,'name':'测试'+c} for c in codes],
                        'routes':{c:{'primary':'山东仓','secondary':None} for c in codes},'profile':None})
    catalog=args.inputs/'catalog.xlsx'
    digest=load_catalog(repo,catalog,'山东仓',expected=file_hash(catalog),expected_rows=2)
    run=repo.new_run();repo.change(run['id'],catalogs={'山东仓':digest},external={'catalogs_frozen':True})
    spec=importlib.util.spec_from_file_location('measured_acquisition',args.module)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    platform=OfflinePlatform(folder/'native',args.inputs,codes);progress=[]
    proc=psutil.Process();io_before=proc.io_counters();peak=[proc.memory_info().rss];stop=threading.Event()
    def monitor():
        while not stop.wait(.02):peak[0]=max(peak[0],proc.memory_info().rss)
    watcher=threading.Thread(target=monitor);watcher.start();started=time.monotonic()
    try:result=module.execute(repo,run['id'],platform.rpc,threading.Event(),progress.append)
    finally:elapsed=time.monotonic()-started;stop.set();watcher.join()
    after=proc.io_counters();assert result['state']=='COMPLETE'
    with repo.connect() as db:
        columns=[r[1] for r in db.execute('PRAGMA table_info(facts)') if r[1]!='snapshot']
        facts=[tuple(r) for r in db.execute('SELECT s.store,s.day,s.observed,s.source_sha,s.rows,'+','.join('f.'+c for c in columns)+
            ' FROM store_snapshots s JOIN facts f ON f.snapshot=s.id ORDER BY s.store,f.sku')]
    totals={v:query(repo,{'start':'2026-11-07','end':'2026-11-07','view':v})['total'] for v in ['all','retired','assessment','both']}
    signature=hashlib.sha256(json.dumps({'facts':facts,'totals':totals},ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    for event in platform.events:event['seconds']=event.pop('at')-started
    scopes=[r['request']['scope']['warehouse_codes'] for r in platform.commands.values()]
    report={'count':args.count,'seconds':elapsed,'command_scopes':scopes,'events':platform.events,'progress':progress,
            'peak_rss':peak[0],'read_bytes':after.read_bytes-io_before.read_bytes,'write_bytes':after.write_bytes-io_before.write_bytes,
            'business_signature':signature,'totals':totals,'complete_stores':[s['store'] for s in result['stores']]}
    write(folder/'report.json',report)


def main(args):
    from tests.test_stockout_analysis import workbook
    out=args.output.resolve();out.mkdir(parents=True,exist_ok=False);inputs=out/'inputs';inputs.mkdir()
    baseline=subprocess.check_output(['git','rev-parse',args.baseline+'^{commit}'],cwd=ROOT,text=True).strip()
    old=out/'baseline_acquisition.py';old.write_bytes(subprocess.check_output(['git','show',baseline+':src/stockout_acquisition.py'],cwd=ROOT))
    workbook(inputs/'catalog.xlsx',['商品编码','分类','商品状态','可用数','供应商名称','虚拟分类'],
             [['A','食品','启用',5,'供货商甲','标杆淘汰'],['B','饮品','启用',5,'供货商乙','淘便考核']])
    for code in ['S5','S1','S3','S2','S4']:
        workbook(inputs/(code+'.xlsx'),['仓库/门店编码','商品编码','商品名称','月销','可用库存','在途库存'],
                 [[code,'A','商品A',4,0,6],[code,'B','商品B',2,3,0],[code,'L','地采品',5,0,0]])
    raw=[];summaries=[]
    for count in [1,3,5]:
        expected=None
        for round_index in range(3):
            for version in (['baseline','candidate'] if round_index%2==0 else ['candidate','baseline']):
                folder=out/f'{count}-{round_index}-{version}'
                module=old if version=='baseline' else ROOT/'src/stockout_acquisition.py'
                subprocess.run([sys.executable,__file__,'--output',str(folder),'--sample','--inputs',str(inputs),
                                '--module',str(module),'--count',str(count)],cwd=ROOT,check=True)
                value=json.loads((folder/'report.json').read_text('utf-8'));value.update(version=version,round=round_index)
                if expected is None:expected=value['business_signature']
                assert value['business_signature']==expected,'Original hashes, typed fact values or four-view totals differ'
                if version=='candidate':
                    assert len(value['command_scopes'])==1
                    assert [e['action'] for e in value['events'][:count]]==['submit']*count
                raw.append(value)
        summary={'stores':count}
        for version in ['baseline','candidate']:
            values=[r for r in raw if r['count']==count and r['version']==version]
            summary[version]={k:statistics.median(v[k] for v in values) for k in ['seconds','peak_rss','read_bytes','write_bytes']}
        summary['reduction_percent']=100*(1-summary['candidate']['seconds']/summary['baseline']['seconds']);summaries.append(summary)
        print(json.dumps(summary,ensure_ascii=False),flush=True)
    write(out/'report.json',{'passed':True,'baseline_commit':baseline,'candidate_file_sha256':hashlib.sha256((ROOT/'src/stockout_acquisition.py').read_bytes()).hexdigest(),
          'live_platform':False,'method':'Separate processes, same XLSX bytes, sequential alternating 3 runs each; simulated submit 40ms/store, generation 1000ms first store and 600ms others.',
          'equivalence':'Source hashes, all typed fact columns except snapshot identity, and all four view totals match exactly.',
          'limits':'Host scheduling simulation; not actual AX speed, production throughput or low-spec business-PC memory. Native JS pipeline has separate runtime tests.',
          'summaries':summaries,'raw':raw})


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--baseline',default='14f4aa7');parser.add_argument('--sample',action='store_true')
    parser.add_argument('--inputs',type=Path);parser.add_argument('--module',type=Path);parser.add_argument('--count',type=int)
    args=parser.parse_args();sample(args) if args.sample else main(args)
