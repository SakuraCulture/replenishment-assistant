"""Real Qt natural payment and offline purchase-file preparation; new data only."""
import argparse,copy,hashlib,json,os,shutil,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/p) for p in ('src','tests','tools')]
def main():
    parser=argparse.ArgumentParser();parser.add_argument('--out',type=Path,required=True);parser.add_argument('--exe',type=Path);a=parser.parse_args()
    out=a.out.resolve();assert not out.exists()
    import jst_qa,jst_orders as jo,order_lifecycle as lc,warehouse_catalog as wc,operations_flow as flow
    path=jst_qa.fixture(out,2);heads=lc.current_targets(path);flow.adopt(path,heads)
    for wh,c in wc.Repository(path.parent).catalogs().items():
        for row in c['items'].values():row['商品标签']=''
        c['revision']=wc.revision(c['items']);wc.Repository(path.parent).save(c)
        jo.commit_refresh(path,jo.prepare_refresh(path,wh,catalog=c))
    generated=jo.commit(path,jo.prepare(path,heads));assert not generated['errors'] and len(generated['generated'])==4
    contract=json.loads((ROOT/'components/aoxiang/assets/purchase-import-template-contract.json').read_text('utf-8'))
    template=out/'synthetic-purchase-template.xlsx';shutil.copyfile(ROOT/'components/aoxiang/test/fixtures/purchase-import-template.xlsx',template)
    config={'configured':True,'enabled':True,'account_binding':{'account_id':'SYN-A','merchant_code':'SYN-M'},'store_mapping_version':'synthetic-v1',
            'targets':[{'buyerWarehouseCode':h['store'],'displayName':h['name']} for h in heads[::2]]}
    # Deduplicate by exact store code, never by display name.
    config['targets']=list({h['store']:{'buyerWarehouseCode':h['store'],'displayName':h['name']} for h in heads}.values())
    receipt={'status':'SUPPORTED','file':str(template),'sha256':hashlib.sha256(template.read_bytes()).hexdigest(),'contractVersion':contract['version'],'schema':copy.deepcopy(contract['schema']),
             'identity':{'accountId':'SYN-A','merchantCode':'SYN-M'},'recordFile':str(out/'template-record.json'),'attemptId':'offline-synthetic','importSubmissions':0,'source':'marked offline fixture, not platform'}
    (out/'template-record.json').write_text(json.dumps(receipt,ensure_ascii=False),'utf-8')
    (out/'fixture.json').write_text(json.dumps({'heads':heads,'configuration':config,'template':receipt,'gids':generated['generated']},ensure_ascii=False),'utf-8')
    (out/'synthetic.json').write_text(json.dumps({'synthetic':True,'live_platform':False}),'utf-8')
    env=os.environ.copy()
    for name in ('PYTHONPATH','PYTHONHOME','QT_PLUGIN_PATH','REPLENISHMENT_DATA_DIR','REPLENISHMENT_PORTABLE_PATHS','QT_QPA_PLATFORM'):env.pop(name,None)
    env.update(REPLENISHMENT_DATA_DIR=str(out/'settings'),LOCALAPPDATA=str(out/'localappdata'),APPDATA=str(out/'roaming'),PYTHONUTF8='1')
    if not a.exe:env['QT_QPA_PLATFORM']='offscreen'
    command=[str(a.exe.resolve())] if a.exe else [sys.executable,str(ROOT/'src/app.py')]
    report={'passed':False,'runs':[],'synthetic':True,'live_platform':False,'exe_sha256':hashlib.sha256(a.exe.read_bytes()).hexdigest() if a.exe else None}
    for mode in ('exercise','reopen'):
        with (out/(mode+'-process.log')).open('wb') as log:
            process=subprocess.Popen([*command,'--lifecycle-followup-smoke',str(out),mode],cwd=out,env=env,stdout=log,stderr=log)
            try:code=process.wait(240)
            except subprocess.TimeoutExpired:
                report.update(error='Owned process retained; no forced exit',pid=process.pid);(out/'report.json').write_text(json.dumps(report),'utf-8');raise
        file=out/(mode+'-report.json');result=json.loads(file.read_text('utf-8')) if file.exists() else {'passed':False,'error':'No native report'}
        result['exit_code']=code;report['runs'].append(result);(out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
        assert code==0 and result['passed'],result
    report['passed']=True;(out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
    print(json.dumps({'passed':True,'checks':sum(len(r['checks']) for r in report['runs']),'out':str(out)},ensure_ascii=False))
if __name__=='__main__':main()
