import json,sys,time,unittest
from pathlib import Path
ROOT=Path(r"C:\Users\lenovo\.codex\worktrees\data-location-0521\replenishment_mvp_20261029")
sys.path[:0]=[str(ROOT/"src"),str(ROOT/"tests")]
OUT=Path(__file__).resolve().parent
NAMES=["test_workflow_recovery","test_allocation_recovery","test_lifecycle_recovery","test_lifecycle","test_lifecycle_batch","test_operations_lifecycle","test_jst_batches","test_paid_return_reallocation","test_warehouse_execution","test_warehouse_execution_delete","test_deletion_workflow_consistency","test_export_recovery","test_signing_failures","test_signed_overwrite","test_aoxiang_workflows","test_aoxiang_selected_queue","test_purchase_recovery","test_purchase_pipeline","test_jst_batch_recovery","test_jst_results","test_jst_payment","test_jst_review","test_lifecycle_performance","test_lifecycle_followup_performance","test_queue_lock_recovery"]
def flatten(s):
 for t in s:
  if isinstance(t,unittest.TestSuite):yield from flatten(t)
  else:yield t
def main():
    unique={}
    for name in NAMES:
     for test in flatten(unittest.defaultTestLoader.loadTestsFromName(name)):unique.setdefault(test.id(),test)
    started=time.monotonic()
    with (OUT/"targeted-03.log").open("w",encoding="utf-8") as log:
     result=unittest.TextTestRunner(stream=log,verbosity=2).run(unittest.TestSuite(unique.values()))
    report={"passed":result.wasSuccessful(),"tests":result.testsRun,"unique_ids":list(unique),"seconds":time.monotonic()-started,"failures":[{"id":t.id(),"text":e} for t,e in result.failures],"errors":[{"id":t.id(),"text":e} for t,e in result.errors],"skips":[{"id":t.id(),"reason":e} for t,e in result.skipped],"synthetic":True,"live_platform":False}
    (OUT/"targeted-03.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),"utf-8")
    print(json.dumps({k:v for k,v in report.items() if k!="unique_ids"},ensure_ascii=False));sys.exit(0 if result.wasSuccessful() else 1)

if __name__=='__main__':main()
