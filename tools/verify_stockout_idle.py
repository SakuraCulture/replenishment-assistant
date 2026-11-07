"""Alternating serial baseline/candidate cold checks, with unused synthetic history."""
import argparse,json,os,subprocess,sys,zipfile,statistics
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]


def main(a):
    out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
    archive=out/'baseline.zip';subprocess.run(['git','archive','--format=zip','-o',str(archive),a.baseline,'src'],cwd=ROOT,check=True)
    baseline=out/'baseline'
    with zipfile.ZipFile(archive) as z:z.extractall(baseline)
    records=[]
    for scale,db in [('empty',None),('30-day',a.capacity/'30-day.sqlite3'),('90-day',a.capacity/'settings/analysis/stockout/stockout.sqlite3')]:
        for n in range(3):
            for side in (['baseline','candidate'] if n%2==0 else ['candidate','baseline']):
                folder=out/f'{scale}-{side}-{n+1}';folder.mkdir();(folder/'synthetic.json').write_text('{"synthetic":true}','utf-8')
                if db:
                    target=folder/'settings/analysis/stockout/stockout.sqlite3';target.parent.mkdir(parents=True)
                    os.link(db.resolve(),target)
                with (folder/'process.log').open('w',encoding='utf-8') as log:
                    subprocess.run([sys.executable,str(ROOT/'tools/stockout_idle_probe.py'),'--source',str(baseline if side=='baseline' else ROOT),'--output',str(folder)],
                        cwd=ROOT,stdout=log,stderr=log,env={**os.environ,'PYTHONIOENCODING':'utf-8'},check=True)
                report=json.loads((folder/'report.json').read_text('utf-8'));assert report['passed']
                records.append({'scale':scale,'side':side,'round':n+1,**report});print(scale,side,n+1,round(report['startup_seconds'],3),flush=True)
    summary={}
    for scale in ('empty','30-day','90-day'):
        summary[scale]={side:{k:statistics.median(r[k] for r in records if r['scale']==scale and r['side']==side)
                              for k in ('startup_seconds','business_reads_100_seconds','rss','peak_rss')} for side in ('baseline','candidate')}
    (out/'report.json').write_text(json.dumps({'passed':True,'baseline':a.baseline,'summary':summary,'records':records,
        'limits':'Same-machine synthetic unused history, Python audit of analysis file/SQLite opens, empty original business reads. Does not measure low-spec PC or physical disk utilization.'},ensure_ascii=False,indent=2),'utf-8')


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);p.add_argument('--capacity',type=Path,required=True);p.add_argument('--baseline',required=True)
    main(p.parse_args())
