"""Serial before/after measurements on isolated current-format fixtures only."""
import argparse,json,os,subprocess,sys
from contextlib import closing
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];QA=ROOT/'qa/current_storage_0461_rc2'
def run(name,fixture,rounds,parents=1,warm=None):
    cmd=[sys.executable,str(ROOT/'tools/verify_deletion_load.py'),'run','--fixture',str(fixture),'--output',str(QA/name),'--qa-root',str(QA),'--rounds',str(rounds),'--parents',str(parents)]
    if warm:cmd+=['--warm',str(warm)]
    subprocess.run(cmd,cwd=ROOT,check=True,env={**os.environ,'PYTHONIOENCODING':'utf-8','PYTHONHASHSEED':'0'})
def main():
    global QA
    parser=argparse.ArgumentParser();parser.add_argument('--qa-root',type=Path,default=QA);a=parser.parse_args()
    QA=a.qa_root.resolve();QA.relative_to(ROOT/'qa');inputs=json.loads((QA/'inputs.json').read_text('utf-8'))
    for name in ('fixture','synthetic'):
        if not (Path(inputs[name])/'workbench.sqlite3').is_file():raise ValueError(name+' must point to the settings folder')
    fixture=Path(inputs['fixture']);warm=Path(inputs['warm'])
    # Cold/multiple are paired checks; warm/synthetic retain three alternating
    # rounds. The report must disclose the different sample counts.
    run('cold',fixture,1)
    run('warm',fixture,3,warm=warm)
    run('multiple',fixture,1,parents=3,warm=warm)
    run('synthetic-performance',Path(inputs['synthetic']),3)
if __name__=='__main__':main()
