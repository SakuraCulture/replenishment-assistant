"""Exercise the immutable packaged UI module using isolated Edge, no platform."""
import argparse,json,subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def main():
    p=argparse.ArgumentParser();p.add_argument('--component',type=Path,required=True);p.add_argument('--out',type=Path,required=True);a=p.parse_args()
    component=a.component.resolve();out=a.out.resolve();out.relative_to(ROOT/'qa');out.mkdir(parents=True,exist_ok=False)
    test=ROOT/'components/aoxiang/test/export-menu-recovery.test.mjs'
    source=test.read_text('utf-8').replace("'../src/aoxiang-ui.mjs'",repr((component/'src/aoxiang-ui.mjs').as_uri())).replace("'./edge-fixture.mjs'",repr((test.parent/'edge-fixture.mjs').as_uri()))
    script=out/'packaged.test.mjs';script.write_text(source,'utf-8')
    with (out/'node.log').open('w',encoding='utf-8') as f:
        r=subprocess.run([str(component/'runtime/node.exe'),'--test',str(script)],stdout=f,stderr=subprocess.STDOUT,timeout=90)
    assert r.returncode==0,(out/'node.log').read_text('utf-8')
    (out/'acceptance.json').write_text(json.dumps({'passed':True,'live_platform':False,'packaged_module':str(component/'src/aoxiang-ui.mjs'),'tests':3}),'utf-8')
if __name__=='__main__':main()
