"""Re-runnable history projection/index equivalence; real snapshots + corrupt cache."""
import sys,json,sqlite3,threading,importlib.util,hashlib,tempfile,shutil
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
import history_read as h,work_repository as wr

def main():
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path);args=parser.parse_args()
    qa=ROOT/'qa/archive_072';db=qa/'snapshot.sqlite3';out=args.output or qa/'stage_b';out.mkdir(exist_ok=False);cache=out/'index.sqlite3';repo=h.repository(db);heads=repo.headers();legacy=repo.list();cancel=threading.Event();checks=[]
    data=h.execute(db,cache,{'kind':'list'},cancel,lambda p:None);assert wr.dumps(data['records'])==wr.dumps(legacy)
    expected_search=[]
    for head in heads:
        order=repo.get(head['day'],head['store']);view=h.project(order)
        actual=h.execute(db,cache,{'kind':'detail','head':head},cancel,lambda p:None);assert wr.dumps(view)==wr.dumps(actual)
        assert len(actual['rows'])==len(order['payload']['result']['rows']);expected_search.append(view['search'])
        assert wr.dumps(order)==wr.dumps(repo.get(head['day'],head['store']))
    queries=[json.dumps(legacy[0],ensure_ascii=False)[:100],'','无匹配QA词','0','00','商品','_','%','[','2026',legacy[0]['store'],legacy[0]['name'],legacy[0]['number'],'001']
    for q in queries:
        actual=h.execute(db,cache,{'kind':'list','search':q},cancel,lambda p:None)
        wanted=[q.lower() in json.dumps(r,ensure_ascii=False).lower() or any(q.lower() in t for t in search) for r,search in zip(legacy,expected_search)]
        assert actual['matches']==wanted,q;assert actual['records']==legacy
        assert [list(r) for r in actual['records']]==[list(r) for r in legacy]
    # Deliberately damage only derived data; rebuild must match original.
    with sqlite3.connect(cache) as c:c.execute("UPDATE views SET summary='invalid json'")
    assert h.execute(db,cache,{'kind':'list'},cancel,lambda p:None)['records']==legacy
    bad=out/'bad.sqlite3';bad.write_bytes(b'broken index');assert h.execute(db,bad,{'kind':'list'},cancel,lambda p:None)['records']==legacy
    cancel.set()
    try:h.execute(db,cache,{'kind':'list'},cancel,lambda p:None);raise AssertionError('cancel accepted')
    except InterruptedError:pass
    report={'passed':True,'ordered_full_projection_differences':0,'real_orders':len(heads),'rows':sum(len(x) for x in expected_search),'queries':queries,'checks':['cache missing/cold','warm full ordered projection','literal substring query semantics','damaged row checksum rebuild','broken cache file rebuild','cancel','source-only read'],'whitelist':[]}
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(report,ensure_ascii=False))
if __name__=='__main__':main()
