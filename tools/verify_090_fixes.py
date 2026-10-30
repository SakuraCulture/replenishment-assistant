"""Deterministic cancellation boundaries, equal-money preview and causal reasons."""
import sys,json,copy,threading,sqlite3,argparse,time
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
import work_repository as wr,normal_orders as no,normal_delivery as nd,normal_exports as ne
import model,order_review,result_semantics as sem,storage_chunks as chunks
from runtime_support import Cancelled,CommitProgress

def main():
    a=argparse.ArgumentParser();a.add_argument('--fixture',type=Path,required=True);a.add_argument('--output',type=Path,required=True);args=a.parse_args();out=args.output;out.mkdir(parents=True,exist_ok=False)
    p=wr.loads(args.fixture.read_bytes());p['day']=wr.today();codes=list(p['results']);checks=[];sideeffects=[]
    def counts(work):
        with work.connect() as db:return {t:db.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ['jobs','normal_orders','normal_revisions','normal_work_revisions','normal_events']}
    for label,trigger in [('encoding','编码内容块'),('backup','备份升级前数据库'),('before_begin','等待归档写锁'),('after_lock','已取得写锁'),('before_commit','事务内容准备完成'),('after_boundary',None)]:
        work=wr.Repository(out/label);repo=no.Repository(work);cancel=threading.Event();fired=[]
        def progress(msg):
            if (trigger and str(msg).startswith(trigger)) or label=='after_boundary' and isinstance(msg,CommitProgress) and msg.active:
                fired.append(str(msg));cancel.set()
        try:repo.confirm(p,codes,None,{s:None for s in codes},cancel=cancel,progress=progress,headers_only=True)
        except Cancelled:assert label!='after_boundary'
        else:assert label=='after_boundary'
        assert fired,label
        result=counts(work)
        if label=='after_boundary':assert result['normal_orders']==len(codes)
        else:
            assert all(v==0 for v in result.values()),result
            cancel.clear();repo.confirm(p,codes,None,{s:None for s in codes},cancel=cancel,headers_only=True)
            assert counts(work)['normal_orders']==len(codes)
        sideeffects.append({'case':label,'backups':[x.name for x in work.path.parent.glob('*before*')],'business_before_retry':result})
        checks.append(label+' and retry')
    work=wr.Repository(out/'locked');repo=no.Repository(work);cancel=threading.Event();timers=[];locker=sqlite3.connect(work.path,check_same_thread=False)
    def locked(msg):
        if str(msg).startswith('等待归档写锁'):
            locker.execute('BEGIN IMMEDIATE')
            for delay,fn in [(.15,cancel.set),(.5,locker.rollback)]:
                t=threading.Timer(delay,fn);timers.append(t);t.start()
    try:repo.confirm(p,codes,None,{s:None for s in codes},cancel=cancel,progress=locked)
    except Cancelled:pass
    else:raise AssertionError('waiting-lock cancellation committed')
    for t in timers:t.join()
    locker.close();assert all(v==0 for v in counts(work).values());checks.append('waiting write lock cancels and leaves all business tables empty')
    # Exact accepted synthetic example: same money, changed SKU allocation.
    work=wr.Repository(out/'preview');repo=no.Repository(work);before=copy.deepcopy(p)
    before['results']['QA_A']=copy.deepcopy(p['results']['QA_B']);r=before['results']['QA_A'];r['store']='QA_A';r['store_name']=p['results']['QA_A']['store_name']
    for d in r['rows']:d['B']='QA_A';d['A']=r['store_name']
    repo.confirm(before,['QA_A'],None,{'QA_A':None});old=repo.get(p['day'],'QA_A')
    review=order_review.build(old,p['results']['QA_A'],p['new_focus']);text=order_review.preview([review])['detail_text']
    assert review['before_amount']==review['after_amount']==12
    pairs={x[0]:(x[3],x[4]) for x in review['goods']}
    assert pairs['moving']==(5,0) and pairs['new_must']==(0,6) and pairs['new_anomaly']==(1,0)
    assert all(k in text for k in pairs);checks.append('same 12 yuan preview shows moving 5→0, new_must 0→6, new_anomaly 1→0')
    reason_cases=[]
    def check(name,row,expected):
        actual=ne.decision(row);assert actual==expected,(name,actual,expected);reason_cases.append({'case':name,'reason':actual,'AR':row.get('AR'),'BQ':row.get('BQ'),'AS':row.get('AS')})
    base=model.calculate(dict(H=5,I=35,J=150,K=0,L=0,N=0,O=6,V=2,S='启用',AB=.4,AC=.6,AD=.2))
    base.update(_meta={'blocking':[]},_source='资料匹配')
    assert base['AR']==78 and base['BQ']==6 and base['AS']==78
    check('ordinary 78 above floor 6',base,'动销补货')
    for step in [1,6]:
        equal={**base,'AR':step,'BI':1,'BP':step,'BQ':step,'BR':step,'BU':step,'AS':step}
        check('equal no uplift '+str(step),equal,'动销补货')
        check('floor uplift '+str(step),{**equal,'AR':0,'BI':0},'最低持有')
        check('zero sales restore '+str(step),{**equal,'AR':0,'BI':0,'J':0,'N':10},'零销恢复')
    check('extreme',{**base,'AW':'极端缺货自动拉回'},'极端缺货恢复')
    check('one box',{**base,'_audit':{'applied':True}},'一箱恢复')
    check('new',{**base,'J':0,'K':0,'L':0,'N':0,'_new_requested':6},'新品首铺')
    check('budget partial',{**base,'_budget_gap':1,'_execution_qty':6},'预算限量')
    check('budget deferred',{**base,'_budget_gap':1,'_execution_qty':0},'预算暂缓')
    check('legacy incomplete',{k:v for k,v in base.items() if k!='BU'},'补货建议')
    report={'passed':True,'checks':checks,'side_effects':sideeffects,'cancellation_boundary':'last checkpoint immediately before CommitProgress(True) and db.commit; later cancel retains commit','reason_cases':reason_cases,'allowed_persisted_calculation_field_changes':[]}
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8',newline='\n');print(json.dumps(report,ensure_ascii=False))

if __name__=='__main__':main()
