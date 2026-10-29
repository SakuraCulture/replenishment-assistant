"""Owned snapshot transport and isolated daily I/O preserve exact values/order/types."""
import sys,json,tempfile,copy,threading,multiprocessing,hashlib,traceback
from pathlib import Path
from datetime import datetime,date,timezone
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tools')]
from work_transfer import send_tree,receive_tree
from ui_work_io import Session
import work_repository as wr
from parallel_qa import diff
from version_info import MODEL_VERSION


def echo(c):
    try:v,stats=receive_tree(c);send_tree(c,{'value':v,'stats':stats})
    finally:c.close()


def main():
    q=ROOT/'qa/full_workflow_073/transfer_final';q.mkdir(exist_ok=False);report={'passed':False,'checks':[]}
    try:
        ctx=multiprocessing.get_context('spawn');a,b=ctx.Pipe();p=ctx.Process(target=echo,args=(b,));p.start();b.close()
        special={'empty':None,'zero':0,'negative_zero':-0.0,'blank':'','待核':'待核','id':'000012345678901234567890','bool':False,'int':2**70,'float':0.12345678901234568,'date':date(2026,10,30),'time':datetime(2026,10,30,3,2,1,8,tzinfo=timezone.utc),'tuple':(8,5),'list':[],'dict':{}}
        tree={'rows':[{str(i):copy.deepcopy(special)} for i in range(1800)],'products':{str(i):copy.deepcopy(special) for i in range(700)},'other':special}
        tree['rows'][0]['nested']=[str(i)+'x'*600 for i in range(2000)]
        send_tree(a,tree);response,_=receive_tree(a);assert diff(tree,response['value'])==[];p.join(10);assert p.exitcode==0;p.close();a.close();report['transport']=response['stats'];report['checks'].append('nested frames preserve key/list order, None/0/blank/text/bool/int/float/date/datetime/tuple exactly')
        repo=wr.Repository(q/'settings');day=wr.today();payload={'schema':1,'day':day,'model_version':MODEL_VERSION,'states':{},'results':{},'input_dirty':True,'catalogs':{},'profile':None,'loaded':None,'store_presets':{},'store_ui':{},'test':tree}
        with Session(repo.path) as session:
            plan=session.call('prepare_save',payload,lambda _:None);assert plan=={'old':None,'same':False};receipt=session.call('save',None,lambda _:None)
        old=repo.get_job(day);assert wr.dumps(old['payload'])==wr.dumps(payload);first_digest=old['digest']
        with Session(repo.path) as session:
            plan=session.call('prepare_save',payload,lambda _:None);assert plan['same'];assert plan['old']['digest']==first_digest
        changed=copy.deepcopy(payload);changed['test']['other']['blank']='changed'
        with Session(repo.path) as session:
            plan=session.call('prepare_save',changed,lambda _:None);assert not plan['same']
            # Close without save is the exact same-day overwrite-cancel path.
        assert repo.get_job(day)['digest']==first_digest
        with Session(repo.path) as session:
            session.call('prepare_save',changed,lambda _:None);external=copy.deepcopy(payload);external['outside']=1;repo.save_job(day,external,receipt['revision'])
            try:session.call('save',None,lambda _:None)
            except RuntimeError as exc:assert '另一窗口' in str(exc)
            else:raise AssertionError('CAS was bypassed')
        assert repo.get_job(day)['payload']['outside']==1
        with Session(repo.path) as session:
            record=session.call('load',day,lambda _:None);assert diff(record,repo.get_job(day))==[]
            config=session.call('copy_config',day,lambda _:None);assert config=={'store_presets':{},'store_ui':{}}
        with Session(repo.path) as session:
            session.call('prepare_save',changed,lambda _:None);session.call('save',None,lambda _:None)
        assert wr.dumps(repo.get_job(day)['payload'])==wr.dumps(changed)
        report['checks']+=['first save digest equals existing Repository','unchanged save','overwrite cancel leaves original digest','stale revision refuses overwrite and preserves external save','load/config projection values and types exact','confirmed overwrite reuses existing transaction']
        report.update(passed=True,differences=0)
    except Exception:report['error']=traceback.format_exc()
    (q/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(report,ensure_ascii=False));sys.exit(0 if report['passed'] else 1)
if __name__=='__main__':multiprocessing.freeze_support();main()
