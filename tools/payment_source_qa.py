"""Repeatable isolated 0.22 benchmark, ordered generation/export comparison and EXE."""
import argparse,hashlib,json,os,shutil,statistics,subprocess,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))
from jst_qa import fixture,measure,save,sha


def worker(source,out):
    sys.path.insert(0,str(source/'src'))
    import jst_orders as j,jst_exports as ex,order_lifecycle as lc,openpyxl
    path=out/'settings/workbench.sqlite3';heads=lc.current_targets(path);times={}
    start=time.perf_counter()
    for wh,c in j.catalogs(path,out/'settings').items():j.commit_refresh(path,j.prepare_refresh(path,wh,catalog=c['catalog']))
    times['stock_prepare']=time.perf_counter()-start
    start=time.perf_counter();plan=j.prepare(path,heads);times['allocation_preview']=time.perf_counter()-start
    start=time.perf_counter();r=j.commit(path,plan);times['generation_save']=time.perf_counter()-start
    start=time.perf_counter();files=ex.export(path,r['generated'],out/'exports')['files'];times['export']=time.perf_counter()-start
    start=time.perf_counter()
    for gid in r['generated']:j.payment_plan(path,gid)
    times['payment_preview']=time.perf_counter()-start
    facts=[];cells=[]
    for f in files:
        p=j.get(path,f['gid']);facts.append([p['head']['number'],p['rows'],p['summary'],p['basis']])
        b=openpyxl.load_workbook(f['path']);s=b.active
        cells.append({'sheets':b.sheetnames,'rows':s.max_row,'freeze':s.freeze_panes,'first_three':[[[c.value,c.data_type,c.number_format,str(c._style)] for c in row[:3]] for row in s]})
        b.close()
    save(out/'result.json',{'timings':times,'facts':facts,'export_first_three':cells})


def benchmark(qa,out):
    out.mkdir(parents=True,exist_ok=False);env=os.environ.copy();env.update(PYTHONIOENCODING='utf-8',PYTHONHASHSEED='0')
    records=[]
    for stores in (3,10):
        seed=out/f'seed-{stores}';fixture(seed,stores)
        for repeat in range(3):
            pair={}
            for mode in (('old','new') if repeat%2==0 else ('new','old')):
                dest=out/f'{stores}-{repeat}-{mode}';shutil.copytree(seed,dest)
                source=qa/'baseline' if mode=='old' else ROOT
                sample=measure([sys.executable,__file__,'worker','--source',str(source),'--out',str(dest)],dest,env)
                result=json.loads((dest/'result.json').read_text(encoding='utf-8'));pair[mode]=result
                records.append({'stores':stores,'repeat':repeat,'mode':mode,**sample,**result['timings']})
            if pair['old']['facts']!=pair['new']['facts']:raise AssertionError('Ordered generation facts differ')
            if pair['old']['export_first_three']!=pair['new']['export_first_three']:raise AssertionError('First three columns differ')
            save(out/'raw.json',records)
    summary=[]
    for stores in (3,10):
        for mode in ('old','new'):
            rows=[r for r in records if r['stores']==stores and r['mode']==mode]
            keys=['stock_prepare','allocation_preview','generation_save','export','payment_preview','elapsed','peak_tree_mib']
            summary.append({'stores':stores,'mode':mode,'metrics':{k:{'median':statistics.median(r[k] for r in rows),'min':min(r[k] for r in rows),'max':max(r[k] for r in rows)} for k in keys}})
    save(out/'report.json',{'passed':True,'synthetic':True,'rounds':3,'ordered_business_differences':0,'first_three_export_differences':0,'scope':'all generation rows, summary, upstream basis; ordered first 3 Excel columns values/types/styles; fourth column is intentional addition','memory':'50ms process tree RSS sum, shared pages not deduplicated; fresh processes, OS cache not flushed','runs':records,'summary':summary})


def exe(program,out):
    fixture(out);env=os.environ.copy()
    for k in ('PYTHONPATH','PYTHONHOME','QT_QPA_PLATFORM','QT_PLUGIN_PATH'):env.pop(k,None)
    env.update(REPLENISHMENT_DATA_DIR=str(out/'settings'),PYTHONHASHSEED='0')
    env['PATH']=env['WINDIR']+';'+str(Path(env['WINDIR'])/'System32')
    result=measure([str(program.resolve()),'--payment-source-smoke',str(out)],out,env)
    ui=json.loads((out/'payment-ui.json').read_text(encoding='utf-8'))
    save(out/'report.json',{'actual_exe':True,'exe_sha256':sha(program),**result,**ui})
    if not ui['passed']:raise AssertionError(ui)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['worker','benchmark','exe']);p.add_argument('--source',type=Path);p.add_argument('--qa',type=Path,default=ROOT/'qa/payment_0220');p.add_argument('--out',type=Path,required=True);p.add_argument('--exe',type=Path);a=p.parse_args()
    if a.action=='worker':worker(a.source.resolve(),a.out.resolve())
    elif a.action=='benchmark':benchmark(a.qa.resolve(),a.out.resolve())
    else:exe(a.exe,a.out.resolve())
