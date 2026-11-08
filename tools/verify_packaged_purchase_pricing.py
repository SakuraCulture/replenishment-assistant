"""Run isolated host-to-actual-packaged-component tests, never connecting a platform."""
import argparse,json,sys,unittest,subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]
def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--component',required=True);parser.add_argument('--output',required=True);a=parser.parse_args()
    out=Path(a.output).resolve();out.mkdir(parents=True,exist_ok=False)
    import test_aoxiang_purchase_tasks as tests
    component=Path(a.component).resolve();tests.NODE=component/'runtime/node.exe';tests.CONTRACT=component/'assets/purchase-import-template-contract.json'
    # Test fixtures are deliberately excluded from releases. Read the unchanged
    # source fixture, but run EVERY module import and NDJSON process in the bundle.
    original_client=tests.ProcessClient
    tests.ProcessClient=lambda ignored,state:original_client(component,state)
    def node(case,script,*args):
        result=subprocess.run([str(tests.NODE),'--input-type=module','-e',script,*args],cwd=component,capture_output=True,encoding='utf-8')
        if result.returncode:case.fail(result.stderr)
        return result.stdout
    tests.PurchaseTaskTests.node=node
    with (out/'tests.log').open('x',encoding='utf-8') as stream:
        result=unittest.TextTestRunner(stream=stream,verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(tests.PurchaseTaskTests))
    report={'passed':result.wasSuccessful(),'tests':result.testsRun,'component':str(component),'live_platform':False,'synthetic':True,'fixtures':str(tests.COMPONENT/'test/fixtures')}
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n','utf-8')
    print(json.dumps(report,ensure_ascii=False));return 0 if result.wasSuccessful() else 1
if __name__=='__main__':sys.exit(main())
