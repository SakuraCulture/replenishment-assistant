"""Isolated regression for frozen worker failures and exact export recovery."""
import json
import os
from pathlib import Path
import sys
import threading
import time
import traceback
from unittest.mock import patch


def no_reader(connection, path, cancel):
    time.sleep(30)


def closed_reader(connection, path, cancel):
    os._exit(17)


def interrupt_commit(connection, path, cancel, before_commit):
    import normal_orders
    from work_io_process import serve

    confirm=normal_orders.Repository.confirm
    def interrupted(self,*args,progress,**kwargs):
        def report(value):
            progress(value)
            if getattr(value,'active',None) is before_commit:os._exit(18)
        return confirm(self,*args,progress=report,**kwargs)
    with patch.object(normal_orders.Repository,'confirm',interrupted):
        serve(connection,path,cancel)


def before_commit(connection, path, cancel):
    interrupt_commit(connection,path,cancel,True)


def after_commit(connection, path, cancel):
    interrupt_commit(connection,path,cancel,False)


def fail_decode():
    raise MemoryError


class FailingPayload:
    def __deepcopy__(self,memo):return self
    def __reduce__(self):return fail_decode,()


def calculation_fixture(loaded,store,settings,profile):
    return {'store':store,'summary':{'amount':1.5},'rows':[]}


def resource_dispatch(checks):
    from batch_service import run
    import performance_counters

    original=performance_counters.Counters
    for scenario in ('ample','tight','mid-batch'):
        events=[];calls=0
        class Controlled(original):
            def system(self):
                nonlocal calls
                calls+=1
                tight=(scenario=='tight' and calls<=2 or scenario=='mid-batch' and 1<calls<=3)
                return {'commit_limit_bytes':8*1024**3,'commit_total_bytes':(8*1024**3-1024**2) if tight else 2*1024**3}
        jobs={name:{} for name in ('A','B','C')}
        with patch.object(performance_counters,'Counters',Controlled),patch('batch_service.calculate_store',calculation_fixture):
            result=run({'normalized':[]},jobs,None,2,lambda store,state,_,error:events.append((store,state,error)),borrow_input=True)
        assert list(result.keys()) and all(result[name]['summary']['amount']==1.5 for name in jobs)
        assert [store for store,state,_ in events if state=='计算中']==list(jobs),events
        waits=[store for store,state,_ in events if state=='排队中']
        assert waits==({'ample':[],'tight':['A'],'mid-batch':['C']}[scenario]),events
        running=maximum=0
        for _,state,_ in events:
            if state=='计算中':running+=1
            elif state=='已完成':running-=1
            maximum=max(maximum,running)
        assert maximum==2 and running==0,events
    checks.append('资源充足、初始紧张与运行中变紧时保持派发顺序，恢复后仍可同时计算两店')


def exercise(root,invoke,checks):
    from batch_service import run
    from runtime_support import Cancelled
    from normal_orders import Repository as Orders
    from signing_memory_fixture import fixture
    from ui_work_io import Session
    from work_failures import WorkFailure
    from work_repository import Repository
    import export_recovery

    work=Repository(root/'transport')
    payload=b'x'*(3*1024*1024)
    for worker,stall,reason in ((closed_reader,10,None),(no_reader,.4,'TimeoutError')):
        started=time.monotonic()
        with Session(work.path,worker=worker,stall_seconds=stall) as session:
            try:invoke(session,'delivery_probe',payload)
            except WorkFailure as error:
                evidence=error.evidence
                assert evidence['commit_state']=='not_started' and evidence['transport']['worker_exitcode'] is not None, evidence
                if reason:assert evidence['exception_type']==reason,evidence
                assert Path(evidence['diagnostic']).is_file()
            else:raise AssertionError('Broken or stalled transport returned success')
        assert time.monotonic()-started<12
        assert not Orders(work).headers()
    checks.append('发送前断管及停读均结束，保留失败阶段、进程退出码和未提交状态')

    with Session(work.path,worker=no_reader,stall_seconds=20) as session:
        timer=threading.Timer(.5,session.cancel.set);timer.start();started=time.monotonic()
        try:invoke(session,'prepare_normal',payload)
        except Cancelled:pass
        else:raise AssertionError('Blocked write ignored user cancellation')
        finally:timer.cancel();timer.join()
        assert time.monotonic()-started<8
    checks.append('取消阻塞中的只读准备可以回到工作台')

    for worker,committed in ((before_commit,False),(after_commit,True)):
        case=root/('committed' if committed else 'rolled-back');case.mkdir()
        work=Repository(case/'settings');orders=Orders(work)
        data,codes=fixture(12,2,4);target=case/'exports';target.mkdir()
        with Session(work.path,worker=worker) as session:
            invoke(session,'delivery_prepare',[data,codes])
            try:invoke(session,'delivery_execute',str(target))
            except WorkFailure as error:
                assert error.evidence['commit_state']==('committed' if committed else 'unknown'),error.evidence
                task=error.evidence['export_task']
                assert task in str(error)
            else:raise AssertionError('Injected commit interrupt returned success')
        heads=orders.headers();assert len(heads)==(2 if committed else 0),heads
        assert not list(target.iterdir())
        plan=export_recovery.inspect(work.path,task)
        if committed:
            chosen=[item['index'] for item in plan['items']]
            recovered=case/'recovered';recovered.mkdir()
            result=export_recovery.recover(work.path,plan,chosen,recovered)
            assert len(result['paths'])==2 and all(Path(file).is_file() for file in result['paths']),result
            assert orders.headers()==heads
        else:assert all(item['action']=='blocked' for item in plan['items']),plan
    checks.append('实际 SQLite 提交前后断管分别验证回滚和按原修订补文件')

    events=[]
    jobs={'done':{},'oom':{'fixture':FailingPayload()},'queued':{}}
    with patch('batch_service.calculate_store',calculation_fixture):
        out=run({'normalized':[]},jobs,None,1,lambda *args:events.append(args))
    end={store:(state,error) for store,state,_,error in events}
    assert out['done']['summary']['amount']==1.5 and out['oom'] is None
    assert end['done'][0]=='已完成' and end['oom'][0]=='计算失败' and end['queued'][0]=='已停止',end
    if getattr(sys,'frozen',False):
        from application_runtime import current
        logs=list((current().folder/'worker-errors').glob('calculation-*.log'))
        assert any('MemoryError' in f.read_text('utf-8') and 'queues.py' in f.read_text('utf-8') for f in logs),logs
        assert '后台计算进程内存不足' in end['oom'][1],end['oom']
    checks.append('任务解包 MemoryError 保留已完成项，未启动项可重跑')
    resource_dispatch(checks)


def start_smoke(app,win,args):
    from PySide6.QtCore import QTimer
    from ui_work_io import request
    from version_info import diagnostic

    out=Path(args[0]).resolve();spec=json.loads((out/'synthetic.json').read_text('utf-8'))
    if spec.get('synthetic') is not True or spec.get('live_platform') is not False or win.work_repository.path.parent.resolve()!=out/'settings':
        raise ValueError('工作进程恢复测试须使用隔离资料目录')
    win.auto_check.stop();win.jst_generator.timer.stop();win.aoxiang_workflows.timer.stop()
    def run_checks():
        report={'passed':False,'actual_exe':bool(getattr(sys,'frozen',False)),'live_platform':False,'version':diagnostic(),'checks':[]}
        try:
            exercise(out/'fixtures',lambda session,operation,value:request(win,session,operation,value),report['checks'])
            report['passed']=True
        except Exception:report['error']=traceback.format_exc()
        finally:
            (out/'worker_recovery.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
            win.job_dirty=False;win._discard_close=True;win.close()
    QTimer.singleShot(300,run_checks)
