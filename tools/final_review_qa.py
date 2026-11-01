"""0.20 review QA: preserve the baseline and alternate whole-editor timings."""
import argparse,hashlib,json,os,statistics,subprocess,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def sha(p):
    with Path(p).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def save(p,v):Path(p).write_text(json.dumps(v,ensure_ascii=False,indent=2),encoding='utf-8')
def protect(q):
    p=q/'baseline.json';v=json.loads(p.read_text(encoding='utf-8'))
    if v.get('review_protection'):raise ValueError('Protection already frozen; do not reset it')
    for name in ['preissue.py','stocking_lists.py','normal_exports.py','normal_orders.py','result_semantics.py','xlsx_exports.py','review_amounts.py']:
        v['core'][name]=sha(q/'baseline/src'/name)
        if sha(ROOT/'src'/name)!=v['core'][name]:raise ValueError('Protected business file already differs: '+name)
    for p in (ROOT/'交付_0.19.1_rc1').rglob('*'):
        if p.is_file():v['protected'][str(p)]=sha(p)
    p=ROOT/'docs/plans/跨系统自动化闭环待办开发方案_20261031.md'
    if p.exists():v['protected'][str(p)]=sha(p)
    v['review_protection']=True;save(q/'baseline.json',v)
def worker(source,out):
    os.environ['QT_QPA_PLATFORM']='offscreen';os.environ['REPLENISHMENT_DATA_DIR']=str(out/'settings')
    sys.path[:0]=[str(source/'src'),str(source/'tests'),str(ROOT/'qa/export_071/qa-deps')]
    from test_preissue import row,result
    from PySide6.QtWidgets import QApplication,QWidget
    from PySide6.QtCore import Qt
    from preissue_ui import FinalListDialog
    import work_repository as wr
    app=QApplication([]);host=QWidget();host.work_repository=wr.Repository(out/'settings');host.job_day=wr.today();host.input_dirty=False;host.readonly=False;host.preissue_drafts={}
    rows=[row(str(i),24,24,2) for i in range(14244)];host.results={'S1':result(rows)};host.states={'S1':{'name':'合成14244行','state':'已完成'}}
    start=time.perf_counter();d=FinalListDialog(host,['S1']);d.show();app.processEvents();opened=time.perf_counter()-start;d.model.reason='经营判断';times=[]
    for i in range(30):
        start=time.perf_counter();d.model.setData(d.model.index(i,9),48 if i%2 else 0);app.processEvents();times.append(time.perf_counter()-start)
    start=time.perf_counter();d.search.setText('合成商品1');d.filter();app.processEvents();filtered=time.perf_counter()-start
    memory=None
    try:
        import psutil
        memory=psutil.Process().memory_info().rss/2**20
    except ImportError:pass
    save(out/'timing.json',{'rows':len(rows),'synthetic':True,'open_seconds':opened,'edit_seconds':times,'edit_median_seconds':statistics.median(times),'filter_seconds':filtered,'rss_at_end_mib':memory,'scope':'includes GUI callbacks/repaint processing; unchanged synthetic fixture; no model calculation in timing'})
    d.dirty=False;d.reject();d.deleteLater();host.deleteLater();app.processEvents();app.quit()
def benchmark(q,out):
    out.mkdir(parents=True,exist_ok=False);runs={'old':[],'new':[]};env=os.environ.copy();env.update(PYTHONIOENCODING='utf-8',PYTHONHASHSEED='0')
    for i in range(3):
        for mode in (['old','new'] if i%2==0 else ['new','old']):
            target=out/f'{mode}-{i}';target.mkdir();source=q/'baseline' if mode=='old' else ROOT
            with (target/'log.txt').open('w',encoding='utf-8') as log:subprocess.run([sys.executable,__file__,'worker','--qa',str(q),'--source',str(source),'--out',str(target)],env=env,stdout=log,stderr=log,check=True)
            runs[mode].append(json.loads((target/'timing.json').read_text(encoding='utf-8')))
    metrics={mode:{key:({'median':statistics.median(r[key] for r in values),'min':min(r[key] for r in values),'max':max(r[key] for r in values)} if all(r[key] is not None for r in values) else {'unavailable':True}) for key in ('open_seconds','edit_median_seconds','filter_seconds','rss_at_end_mib')} for mode,values in runs.items()}
    save(out/'report.json',{'runs':runs,'metrics':metrics,'environment':'Same Windows/Python/dependencies; offscreen Qt; sequential alternating processes; warm code cache; 14244 synthetic rows, not business timing','memory':'RSS at end, not sampled peak; do not compare with earlier process-tree peak reports'})
def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=['protect','benchmark','worker']);p.add_argument('--qa',type=Path,required=True);p.add_argument('--out',type=Path);p.add_argument('--source',type=Path);a=p.parse_args()
    if a.action=='protect':protect(a.qa.resolve())
    elif a.action=='benchmark':benchmark(a.qa.resolve(),a.out.resolve())
    else:worker(a.source.resolve(),a.out.resolve())
if __name__=='__main__':main()
