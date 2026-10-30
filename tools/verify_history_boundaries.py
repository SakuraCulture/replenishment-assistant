"""Synthetic history growth/revision/audit/cancel tests, separate from real performance."""
import sys,copy,json,sqlite3,threading,uuid,hashlib
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
import history_read as h,normal_orders as n,work_repository as wr,result_semantics as sem
from datetime import date

def main():
    out=ROOT/'qa/archive_072/boundaries';out.mkdir(exist_ok=False);work=wr.Repository(out);repo=n.Repository(work);real=h.repository(ROOT/'qa/archive_072/snapshot.sqlite3');head=real.headers()[0];source=real.get(head['day'],head['store'])['payload']['result']['rows'][:3]
    with work.connect() as db,db:
        for day in range(1,31):
            store='QA_'+str(day%2);rows=copy.deepcopy(source)
            for r in rows:r['B']=store
            result={'store':store,'store_name':'合成店'+store,'rows':rows,'summary':sem.summarize(rows),'created':date(2026,9,day),'version':{'version':'qa'}};oid=str(uuid.uuid4());d=f'2026-09-{day:02d}';payload={'result':result,'new_focus':None};db.execute('INSERT INTO normal_orders VALUES(?,?,?,?,?,?)',(d,store,'normal_model',oid,'QA-'+d,2))
            for rev in [1,2]:db.execute('INSERT INTO normal_revisions VALUES(?,?,?,?,?)',(oid,rev,n.fingerprint(result),d,n.encode(payload)))
    cache=out/'archive_index.sqlite3';cancel=threading.Event()
    def stop(p):
        if p['done']==5:cancel.set()
    try:h.execute(work.path,cache,{'kind':'list'},cancel,stop);raise AssertionError('did not cancel')
    except InterruptedError:pass
    with sqlite3.connect(cache) as db:assert db.execute('SELECT count(*) FROM views').fetchone()[0]==5
    cancel.clear();data=h.execute(work.path,cache,{'kind':'list'},cancel,lambda p:None);assert data['records']==repo.list();assert len(data['records'])==30
    head=repo.headers()[0];old={**head,'revision':1,'current_revision':2};view=h.execute(work.path,cache,{'kind':'detail','head':old},cancel,lambda p:None);assert view['head']['current_revision']==2 and view['head']['revision']==1
    order=repo.get(head['day'],head['store']);target=out/'audit.json';h.execute(work.path,cache,{'kind':'audit_export','head':head,'path':str(target)},cancel,lambda p:None);assert target.read_text(encoding='utf-8')==wr.dumps(order)
    before=hashlib.sha256(target.read_bytes()).hexdigest()
    try:h.execute(work.path,cache,{'kind':'audit_export','head':head,'path':str(target)},cancel,lambda p:None);raise AssertionError('overwrite')
    except FileExistsError:pass
    assert before==hashlib.sha256(target.read_bytes()).hexdigest()
    # Revision index is independent; corrupt source cannot be exported through cache.
    with work.connect() as db,db:db.execute('UPDATE normal_revisions SET payload=? WHERE oid=? AND revision=?',(n.encode({'result':{'rows':[]}}),head['oid'],head['revision']))
    try:h.read_for_export(repo,head);raise AssertionError('bad source accepted')
    except ValueError as e:assert '校验失败' in str(e)
    # Display-only incremental hash must still match original canonical fingerprint.
    from PySide6.QtWidgets import QApplication
    from history_tasks import StatusHashes
    app=QApplication([]);task=StatusHashes({'s':order['payload']['result']},None);answer=[];task.completed.connect(answer.append);task.run();assert answer[0]['s']==n.fingerprint(order['payload']['result'])
    report={'passed':True,'synthetic':True,'days':30,'revisions':60,'checks':['cancel after five atomic cache publications','resume full ordered list','historical current revision binding','full audit JSON byte identity','audit overwrite refused','source corruption rejected by formal export despite derived cache','display streaming digest exact including date encoding']};(out/'report.json').write_text(json.dumps(report,indent=2));print(json.dumps(report))
if __name__=='__main__':main()
