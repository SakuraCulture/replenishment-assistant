"""Alternating sequential old/new runs, including full pool/IPC/ordered merge costs."""
from __future__ import annotations
import argparse,json,os,platform,statistics,subprocess,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))

def js(p,x):Path(p).write_text(json.dumps(x,ensure_ascii=False,indent=2),encoding='utf-8')

def worker(source,q,files,out):
    sys.path.insert(0,str(source/'src'));os.environ['REPLENISHMENT_DATA_DIR']=str(out.parent/'private-settings')
    import batch_service as bs,engine,json,openpyxl
    times={};intake=bs.intake;prepare=bs.prepare
    def measured(name,fn):
        def call(*a,**kw):
            start=time.perf_counter()
            try:return fn(*a,**kw)
            finally:times[name]=time.perf_counter()-start
        return call
    bs.intake=measured('intake',intake);bs.prepare=measured('prepare',prepare)
    # Parent sampler only counts samples between these markers for check-stage peak.
    out.with_suffix('.started').write_text('check',encoding='utf-8')
    started=time.perf_counter();loaded=bs.load(files,q/'inputs/混合仓商品资料.xlsx');times['total']=time.perf_counter()-started
    out.with_suffix('.checked').write_text('done',encoding='utf-8')
    assert not loaded.get('blocked'),'benchmark fixture has unresolved conflicts; do not calculate blocked stores'
    profile=json.loads((q/'inputs/must.json').read_text(encoding='utf-8'));s=next(iter(loaded['stores']));started=time.perf_counter()
    result=bs.calculate_store(loaded,s,{'warehouses':engine.DEFAULT_WAREHOUSES,'exception':True,'budget':3000},profile);times['calculation']=time.perf_counter()-started
    js(out,{'times_seconds':times,'rows':len(loaded['normalized']),'summary':result['summary'],'python':sys.version,'openpyxl':openpyxl.__version__})

def benchmark(q,out,rounds):
    from windows_process_metrics import sample_tree
    from structure_oracle import source_hashes
    source_before=source_hashes(ROOT);commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    out.mkdir(parents=True,exist_ok=False);inp=q/'inputs';scenarios={'single':['补货建议导出服务.xlsx'],'two':[f'part2_{i}.xlsx' for i in range(2)],'six':[f'part6_{i}.xlsx' for i in range(6)],'mixed':['full_reencoded.xlsx',*[f'small{i}.xlsx' for i in range(3)]]}
    records=[];sources={'baseline':q/'baseline','candidate':ROOT}
    for scenario,names in scenarios.items():
        for trial in range(rounds):
            for version in (['baseline','candidate'] if trial%2==0 else ['candidate','baseline']):
                dest=out/f'{scenario}_{trial}_{version}.json';cmd=[sys.executable,str(Path(__file__).resolve()),'worker','--qa',str(q),'--source',str(sources[version]),'--output',str(dest),'--files',*[str(inp/n) for n in names]]
                run=subprocess.Popen(cmd,cwd=ROOT,env={**os.environ,'PYTHONHASHSEED':'0'},stdout=subprocess.PIPE,stderr=subprocess.PIPE);samples=[];peak=0;private=0;child_ids=set();all_peak=0
                while run.poll() is None:
                    sample=sample_tree(run.pid);rss=sum(v['rss'] for v in sample.values());priv=sum(v['private'] for v in sample.values());all_peak=max(all_peak,rss)
                    if dest.with_suffix('.started').exists() and not dest.with_suffix('.checked').exists():
                        peak=max(peak,rss);private=max(private,priv);child_ids.update(i for i in sample if i!=run.pid);samples.append({'time':time.perf_counter(),'rss':rss,'private':priv,'processes':sample})
                    time.sleep(.05)
                stdout,stderr=run.communicate();assert run.returncode==0,(cmd,stdout.decode(errors='replace'),stderr.decode(errors='replace'))
                result=json.loads(dest.read_text(encoding='utf-8'));result.update(scenario=scenario,trial=trial,version=version,check_peak_rss_bytes=peak,check_peak_private_bytes=private,whole_run_peak_rss_bytes=all_peak,child_pids=sorted(child_ids),sample_count=len(samples));js(dest,result);js(dest.with_name(dest.stem+'_samples.json'),samples);records.append(result)
                print(scenario,trial,version,round(result['times_seconds']['total'],3),round(peak/1024**2,1),'MiB',flush=True)
    summary={}
    for scenario in scenarios:
        summary[scenario]={}
        for version in sources:
            group=[r for r in records if r['scenario']==scenario and r['version']==version];metrics={}
            for name in ['intake','prepare','total','calculation','rss_mib','private_mib']:
                values=[r['times_seconds'][name] if name in r['times_seconds'] else r['check_peak_rss_bytes']/1024**2 if name=='rss_mib' else r['check_peak_private_bytes']/1024**2 for r in group]
                metrics[name]={'median':statistics.median(values),'min':min(values),'max':max(values)}
            summary[scenario][version]=metrics
        for field in ['intake','total','rss_mib']:
            a=summary[scenario]['baseline'][field]['median'];b=summary[scenario]['candidate'][field]['median'];summary[scenario][field+'_change_pct']=(b/a-1)*100
    assert all(r['summary']['amount']==2999.87 and r['summary']['positive']==88 and r['rows']==14244 for r in records)
    assert all(not r['child_pids'] for r in records if r['scenario']=='single')
    assert source_before==source_hashes(ROOT),'runtime source changed during benchmark'
    report={'version':json.loads((ROOT/'src/version.json').read_text(encoding='utf-8'))['version'],'source_commit':commit,'candidate_sources':source_before,'passed':True,'rounds':rounds,'summary':summary,'runs':records,'environment':{'platform':platform.platform(),'processor':platform.processor(),'logical_cpu_count':os.cpu_count(),'PYTHONHASHSEED':'0','python':sys.version},'inputs':{s:[{'file':str(inp/n),'bytes':(inp/n).stat().st_size} for n in names] for s,names in scenarios.items()},'method':'New interpreter per run; AB/BA/AB alternating, one run at a time. 50 ms Win32 process-tree WorkingSetSize (RSS) and PrivateUsage samples during bs.load only, including parent and all descendants; RSS sums count shared pages more than once. Misses peaks between samples. Controller excluded. Intake includes master snapshot, file hashes/checking and ordered cross-file merge/dedup. Prepare is subsequent matching/normalization; total measures load call including pool start, IPC, shutdown. OS cache not cleared; repeated warm/mixed-cache timings, not cold disk benchmarks. No concurrent benchmark work. File partitions/excerpts are synthetic layouts of actual rows; mixed uses a same-writer synthetic re-encoding of the full real rows plus three exactly identical 100-row excerpts.'}
    js(out/'performance_report.json',report)
    lines=['# 多文件检查性能实测','',report['method'],'','每格为中位数 [最小, 最大]；秒或MiB。','', '| 场景 | 版本 | 读取检查及归集秒 | 准备匹配秒 | 完整检查秒 | 计算秒 | 采样峰值RSS MiB |','|---|---|---|---|---|---|---|']
    for s in summary:
        for v in sources:
            d=summary[s][v];fmt=lambda n:f"{d[n]['median']:.3f} [{d[n]['min']:.3f}, {d[n]['max']:.3f}]"
            lines.append('| '+' | '.join([s,v,*[fmt(n) for n in ['intake','prepare','total','calculation','rss_mib']]])+' |')
    (out/'性能实测.md').write_text('\n'.join(lines),encoding='utf-8')

def main():
    p=argparse.ArgumentParser();p.add_argument('mode',choices=['run','worker']);p.add_argument('--qa',type=Path,required=True);p.add_argument('--source',type=Path);p.add_argument('--output',type=Path,required=True);p.add_argument('--rounds',type=int,default=3);p.add_argument('--files',nargs='*');a=p.parse_args()
    if a.mode=='worker':worker(a.source.resolve(),a.qa.resolve(),a.files,a.output.resolve())
    else:assert a.rounds>=3;benchmark(a.qa.resolve(),a.output.resolve(),a.rounds)
if __name__=='__main__':
    import multiprocessing
    multiprocessing.freeze_support();main()
