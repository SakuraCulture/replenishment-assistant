"""Stage A: compare immutable real snapshots and legacy summary exactly."""
import sys,importlib.util,sqlite3,json,hashlib,gc,time
from pathlib import Path
from types import SimpleNamespace
root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root/'src'))
import normal_orders as new,work_repository as wr
qa=root/'qa/archive_072'
spec=importlib.util.spec_from_file_location('baseline_normal',qa/'baseline/src/normal_orders.py');old=importlib.util.module_from_spec(spec);spec.loader.exec_module(old)
work=SimpleNamespace(path=qa/'snapshot.sqlite3',connect=lambda:sqlite3.connect((qa/'snapshot.sqlite3').as_uri()+'?mode=ro',uri=True))
a=old.Repository.__new__(old.Repository);a.work=work;b=new.Repository.__new__(new.Repository);b.work=work
assert wr.dumps(a.list())==wr.dumps(b.list())
evidence=[]
for head in b.headers():
    started=time.perf_counter();before=a.get(head['day'],head['store']);t0=time.perf_counter()-started
    started=time.perf_counter();after=b.get(head['day'],head['store']);t1=time.perf_counter()-started
    assert wr.dumps(before)==wr.dumps(after)
    assert new.fingerprint_owned(after['payload']['result'],after['payload'].get('new_focus'))==head['digest']
    evidence.append({'store':head['store'],'rows':len(after['payload']['result']['rows']),'old_seconds':t0,'new_seconds':t1,'full_typed_serialization_equal':True})
    del before,after;gc.collect()
report={'baseline':'a831049a78e41f33369ce06b28709f3080324087','differences':0,'exact_whitelist':[],'orders':evidence,'legacy_list_equal':True}
(qa/'stage_a.json').write_text(json.dumps(report,indent=2),encoding='utf-8');print(json.dumps(report),flush=True)
