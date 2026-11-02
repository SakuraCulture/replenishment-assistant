"""AB/BA/AB isolated read/projection/grid timings; no end-to-end speed claim."""
import argparse,hashlib,json,os,platform,statistics,subprocess,sys,time,threading
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
SCOPES=('决策总览','当时参数','商品完整计算依据')


def worker(a):
    sys.path.insert(0,str(a.source.resolve()/'src'))
    import history_read,history_forms,purchases
    from PySide6.QtWidgets import QApplication
    from business_ui import grid
    app=QApplication([]);repo=history_read.repository(a.db);h=purchases.targets(a.db)[0]
    results={}
    for scope in SCOPES:
        start=time.perf_counter();order=repo.get(h['day'],h['store'],h['revision']);order['warehouse']=h['warehouse'];read=time.perf_counter()
        sku=next(d['E'] for d in order['payload']['result']['rows'] if d.get('AU')==h['warehouse'] and isinstance(d.get('AN'),(int,float)))
        if a.business:
            import business_presenter as v
            rows=v.historical(repo,order,scope,sku);headers=v.HEADERS
        else:rows=history_forms.form(repo,order,scope,sku);headers=['核对项目','当时值','类型']
        projected=time.perf_counter();table,model=grid(None,headers,rows);table.resize(1000,600);table.show();app.processEvents();end=time.perf_counter();table.close();table.deleteLater();app.processEvents()
        results[scope]={'read_seconds':read-start,'projection_seconds':projected-read,'grid_seconds':end-projected,'total_seconds':end-start,'rows':len(rows)}
    print(json.dumps(results,ensure_ascii=False))


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--baseline',type=Path);p.add_argument('--db',type=Path,required=True);p.add_argument('--output',type=Path);p.add_argument('--source',type=Path);p.add_argument('--business',action='store_true');a=p.parse_args()
    if a.source:return worker(a)
    out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);db=a.db.resolve();before=hashlib.sha256(db.read_bytes()).hexdigest();raw=[]
    env=os.environ.copy();env['QT_QPA_PLATFORM']='offscreen';env['PYTHONUTF8']='1'
    for n,pair in enumerate((('old','new'),('new','old'),('old','new')),1):
        for name in pair:
            cmd=[sys.executable,__file__,'--db',str(db),'--source',str(a.baseline.resolve() if name=='old' else ROOT)]+(['--business'] if name=='new' else [])
            r=subprocess.run(cmd,env=env,capture_output=True,text=True,encoding='utf-8',check=True);data=json.loads(r.stdout);raw.append({'round':n,'version':name,'values':data})
    summary={name:{scope:{metric:{'median':statistics.median(v:=[r['values'][scope][metric] for r in raw if r['version']==name]),'min':min(v),'max':max(v)} for metric in raw[0]['values'][scope]} for scope in SCOPES} for name in ('old','new')}
    report={'environment':{'python':sys.version,'os':platform.platform()},'database_sha256':before,'database_unchanged':before==hashlib.sha256(db.read_bytes()).hexdigest(),'raw':raw,'summary':summary,'boundary':'Same synthetic two-store archive, OS file cache not flushed, sequential AB/BA/AB. Timed snapshot read + projection + offscreen grid paint. Imports before first read mostly excluded. No process-start, real-user full-page, cold-index, memory or large-history throughput claim.'}
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(summary,ensure_ascii=False))
    if not report['database_unchanged']:raise AssertionError('Read benchmark changed database')


if __name__=='__main__':main()
