"""Offline receipt/query tests importing every production module from bundle."""
import argparse,json,re,subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def main():
    p=argparse.ArgumentParser();p.add_argument('--component',type=Path,required=True);p.add_argument('--out',type=Path,required=True);a=p.parse_args()
    component=a.component.resolve();out=a.out.resolve();out.mkdir(parents=True,exist_ok=False)
    tests=ROOT/'components/aoxiang/test';names=['purchase-receipts.test.mjs','purchase-known-scope.test.mjs','purchase-exact-ui.test.mjs','purchase-empty-query.test.mjs','purchase-binding.test.mjs']
    for name in [*names,'edge-fixture.mjs']:
        text=(tests/name).read_text('utf-8')
        text=re.sub(r"(['\"])\.\./src/([^'\"]+)\1",lambda m:repr((component/'src'/m[2]).as_uri()),text)
        text=text.replace("'playwright-core'",repr((component/'node_modules/playwright-core/index.mjs').as_uri()))
        (out/name).write_text(text,'utf-8')
    with (out/'tests.log').open('wb') as log:r=subprocess.run([str(component/'runtime/node.exe'),'--test',*[str(out/n) for n in names]],cwd=component,stdout=log,stderr=log)
    raw=(out/'tests.log').read_text('utf-8');m=re.search(r'(?:ℹ|#) tests (\d+)',raw)
    report={'passed':r.returncode==0 and bool(m) and bool(re.search(r'(?:ℹ|#) fail 0',raw)),'tests':int(m[1]) if m else None,'component':str(component),'synthetic':True,'live_platform':False,'scope':'Bundled Node, native receipt fixtures, ledger/recovery, query request contracts, isolated offline Edge form. No vendor page or live export.'}
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8');print(json.dumps(report,ensure_ascii=False));return 0 if report['passed'] else 1
if __name__=='__main__':raise SystemExit(main())
