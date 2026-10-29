"""Sequential alternating real-snapshot read/restore and UI benchmark driver."""
import argparse,json,statistics,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];QA=ROOT/'qa/full_workflow_073'

def stats(values):return {'median':statistics.median(values),'min':min(values),'max':max(values)}

def main():
    p=argparse.ArgumentParser();p.add_argument('--series',required=True);p.add_argument('--mode',choices=['large','ui'],required=True);a=p.parse_args();folder=QA/a.series;assert not folder.exists()
    if a.mode=='ui':return subprocess.run([sys.executable,str(ROOT/'tools/benchmark_workflow_ui.py'),'--series',a.series],check=True)
    folder.mkdir()
    for i,kinds in enumerate([['old','new'],['new','old'],['old','new']],1):
        for kind in kinds:
            subprocess.run([sys.executable,str(ROOT/'tools/probe_large_work_io.py'),'--kind',kind,'--output',str(folder/f'{kind}-{i}')],check=True,cwd=ROOT)
    runs={kind:[json.loads((folder/f'{kind}-{i}/result.json').read_text(encoding='utf-8')) for i in range(1,4)] for kind in ['old','new']};report={}
    for kind,items in runs.items():
        assert all(r['passed'] and not r['remaining_children'] for r in items)
        report[kind]={phase:{k:stats([r[phase][k] for r in items]) for k in ['seconds','max_ui_gap_seconds']} for phase in ['read','restore']}
        report[kind]['combined_seconds']=stats([r['read']['seconds']+r['restore']['seconds'] for r in items]);report[kind]['peak_tree_mib']=stats([max(r['peak_tree_mib'][phase] for phase in ['read','restore']) for r in items])
    report.update(passed=True,runs=3,order=['old/new','new/old','old/new'],identical_payload_digests=list({r['full_payload_digest'] for items in runs.values() for r in items}),scope='5 stores, 84693 result rows, original complete ~970MB canonical daily payload; one run at a time, OS caches not purged; external RSS tree sampling 50ms, shared pages counted per process; validation outside action timings/peaks; UI timer25ms; source Qt offscreen, separate EXE acceptance required')
    assert len(report['identical_payload_digests'])==1;(folder/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(report,ensure_ascii=False))

if __name__=='__main__':main()
