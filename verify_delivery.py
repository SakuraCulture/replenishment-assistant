"""Isolated one-action delivery/CAS/failure regression and actual 4+1 spawn queue."""
import sys,copy,json,subprocess,threading
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parent;sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tools')]
import normal_delivery as nd,normal_orders as no,work_repository as wr,batch_service as bs,engine
from runtime_support import Cancelled
from version_info import VERSION
import queue_probe


def run(out,fixtures):
    out.mkdir(parents=True,exist_ok=False);payload=wr.loads((fixtures/'payload.json').read_bytes());work=wr.Repository(out/'settings');repo=no.Repository(work);checks=[]
    plan=nd.prepare(repo,payload,'QA_A');assert not plan['same'] and not repo.list()
    stop=threading.Event();stop.set()
    try:nd.execute(repo,plan,out/'cancelled.xlsx',cancel=stop)
    except Cancelled:pass
    else:raise AssertionError('pre-sign cancellation ignored')
    assert not repo.list() and not work.get_job(payload['day'])
    (out/'existing.xlsx').write_bytes(b'keep')
    try:nd.execute(repo,plan,out/'existing.xlsx')
    except FileExistsError:pass
    else:raise AssertionError('clobber')
    assert not repo.list()
    messages=[];receipt=nd.execute(repo,plan,out/'signed.xlsx',progress=messages.append);assert receipt['signed'] and receipt['revision']==1;first=repo.get(payload['day'],'QA_A')
    again=nd.prepare(repo,payload,'QA_A');assert again['same'];nd.execute(repo,again,out/'again.xlsx');assert repo.get(payload['day'],'QA_A')['revision']==1
    changed=copy.deepcopy(payload);changed['results']['QA_A']['audit_note']='changed evidence';plan=nd.prepare(repo,changed,'QA_A');assert not plan['same'];nd.execute(repo,plan,out/'v2.xlsx');assert repo.get(payload['day'],'QA_A')['revision']==2
    try:nd.execute(repo,again,out/'stale.xlsx')
    except wr.Conflict:pass
    else:raise AssertionError('stale same revision accepted')
    assert not (out/'stale.xlsx').exists()
    changed['results']['QA_A']['audit_note']='failed export after commit';plan=nd.prepare(repo,changed,'QA_A')
    with patch.object(nd.normal_exports,'export',side_effect=OSError('injected write failure')):
        try:nd.execute(repo,plan,out/'failed.xlsx')
        except RuntimeError as exc:assert '已归档' in str(exc)
        else:raise AssertionError('export failure lost')
    assert repo.get(payload['day'],'QA_A')['revision']==3;assert repo.get(payload['day'],'QA_A',1)['payload']==first['payload'];assert not (out/'failed.xlsx').exists()
    nd.execute(repo,nd.prepare(repo,changed,'QA_A'),out/'retry.xlsx');assert repo.get(payload['day'],'QA_A')['revision']==3
    checks.append('single-action sign/export; cancellation and existing destination before sign preserve DB; same result idempotent; changed result V2; stale revision rejected; post-commit export failure disclosed, history retained, retry no new revision')
    from export_progress import RowProgress
    assert any(isinstance(m,RowProgress) for m in messages)
    batch_repo=no.Repository(wr.Repository(out/'batch-settings'));folder=out/'batch';folder.mkdir()
    batch=nd.prepare_batch(batch_repo,payload,['QA_B','QA_A']);assert [p['store'] for p in batch['plans']]==['QA_B','QA_A']
    try:nd.execute_batch(batch_repo,batch,folder,cancel=stop)
    except Cancelled:pass
    else:raise AssertionError('batch cancellation ignored')
    assert not batch_repo.list()
    receipt=nd.execute_batch(batch_repo,batch,folder);assert len(receipt['paths'])==2 and receipt['signed']
    assert all(batch_repo.get(payload['day'],s)['revision']==1 for s in ['QA_A','QA_B'])
    head=nd.job_head(batch_repo.work,payload['day']);batch=nd.prepare_batch(batch_repo,payload,['QA_B','QA_A'])
    try:nd.execute_batch(batch_repo,batch,folder)
    except FileExistsError:pass
    else:raise AssertionError('batch clobber')
    assert nd.job_head(batch_repo.work,payload['day'])==head
    partial=out/'partial';partial.mkdir();calls=[];export=nd.normal_exports.export
    def fail_second(order,path,**kwargs):
        calls.append(order['store'])
        if len(calls)==2:raise OSError('second store failure')
        return export(order,path,**kwargs)
    with patch.object(nd.normal_exports,'export',side_effect=fail_second):
        try:nd.execute_batch(batch_repo,batch,partial)
        except RuntimeError as exc:assert '1/2' in str(exc) and '未生成' in str(exc)
        else:raise AssertionError('partial failure not disclosed')
    assert len(list(partial.glob('*.xlsx')))==1 and calls==['QA_B','QA_A']
    checks.append('batch preserves selected store order; one transaction; cancel/no-clobber before signing; same revision reused; partial write failure explicitly identifies completed and missing files; full archive retained')
    # Same valid fixture expanded into five explicitly synthetic stores.
    loaded=bs.load([fixtures/'sales.xlsx'],{w:__import__('warehouse_catalog').import_catalog(fixtures/(w+'.xlsx'),w) for w in engine.DEFAULT_WAREHOUSES})
    # Use legacy projected rows without routing; warehouse queue already has its own regressions.
    rows=[x for x in loaded['normalized'] if x[0]['B']=='QA_A'];loaded=copy.deepcopy(loaded);loaded.pop('catalogs',None);loaded.pop('warehouse_rows',None);loaded['normalized']=[];loaded['stores']={}
    for i in range(5):
        code='QA_'+str(i);loaded['stores'][code]='合成店'+str(i)
        for row,meta in rows:
            d=copy.deepcopy(row);d['B']=code;d['A']='合成店'+str(i);loaded['normalized'].append((d,copy.deepcopy(meta)))
    events=[];state={};peak=0
    def notify(s,status,result,error):
        nonlocal peak
        state[s]=status;peak=max(peak,sum(v=='计算中' for v in state.values()));events.append([s,status,dict(state)])
    jobs={s:{'warehouses':engine.DEFAULT_WAREHOUSES,'exception':True,'budget':None} for s in loaded['stores']}
    with patch.object(bs,'calculate_store',queue_probe.calculate):results=bs.run(loaded,jobs,None,4,notify)
    assert all(results.values()) and len(results)==5 and peak==4
    fifth=next(e for e in events if e[:2]==['QA_4','计算中']);assert any(fifth[2].get(s)=='计算中' for s in ['QA_1','QA_2','QA_3']);assert fifth[2]['QA_0']=='已完成'
    checks.append('real Windows spawn 4+1 queue: four active maximum; fifth starts while three slower stores are still running; all five finish without any archive/export call')
    r={'passed':True,'version':VERSION,'source_commit':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),'checks':checks,'queue_events':events,'peak_active':peak,'fixture_note':'five synthetic stores copied from existing acceptance fixture; QA-only unequal start delays, actual calculation function'}
    (out/'delivery_report.json').write_text(json.dumps(r,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(r,ensure_ascii=False))


if __name__=='__main__':
    import argparse,multiprocessing
    multiprocessing.freeze_support();p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);p.add_argument('--fixtures',type=Path,required=True);a=p.parse_args();run(a.output,a.fixtures)
