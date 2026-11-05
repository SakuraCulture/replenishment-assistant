"""AI/CLI adapters for bounded same-snapshot trials and explicit adoption."""
from automation_protocol import CommandError
import stock_diagnosis as service
import stock_diagnosis_adoption as adoption

def require(g,tid):
    session=getattr(g,'diagnosis_sessions',{}).get(tid)
    if session is None:raise CommandError('RESULT_UNAVAILABLE','诊断会话已释放，请重新试算；摘要不能采用')
    return session

def start(g,args):
    key='stock-diagnosis:'+args['operation_key'];existing=g.journal.lookup(key,args)
    if existing:return g.task_record(existing['task_id'])
    g.idle();g.check_context(args['context_token']);snapshot=g.snapshot({});current=dict(g.host.results)
    task,_=g.journal.create(key,args,'stock_diagnosis')
    def finish(session):
        if g.worker.cancel.is_set():g.journal.update(task['task_id'],state='stopped',message='试算已停止，原作业未变');return
        # One complete diagnosis, independent from the general three-result cache.
        g.diagnosis_sessions={task['task_id']:session}
        g.journal.update(task['task_id'],state='completed',message='候选试算完成，尚未采用',candidate_count=len(session['options']))
    g.start_job(task,lambda w:service.run(snapshot,args,current,w.progress.emit,w.cancel),finish);return task

def results(g,args):
    from automation_queries import page
    s=require(g,args['task_id']);options=s['options'];n=args.get('candidate',1)-1
    if not 0<=n<len(options):raise ValueError('候选不存在')
    o=options[n];m=o['comparison']
    return {'session':s['id'],'stale':g.token()!=(s.get('adopted_token') if s['adopted'] else s['context_token']),
        'adopted':s['adopted'],'undone':s.get('undone',False),'rule':s['rule'],'candidate_count':len(options),'candidate':n+1,
        'changes':o['changes'],'stores':m['stores'],'hard_issues':m['hard_issues'],'risk_count':len(m['risks']),
        'recommended':m['recommended'],'note':m['note'],'page':page(m['details'],args.get('offset',0),args.get('limit',50))}

def adopt(g,args):
    s=require(g,args['task_id'])
    # A successful repeat returns its receipt; it must never replay a mutation.
    previous=s.get('adoption_request')
    if previous==args:return s['adoption_receipt']
    if previous is not None:raise CommandError('ALREADY_ADOPTED','此会话已采用；不能改写采用原因或重放')
    g.check_context(args['context_token'])
    receipt=adoption.adopt(g,s,args['candidate']-1,args['reason'],args.get('accept_risks',False))
    s['adoption_request']=dict(args);s['adoption_receipt']=receipt
    return receipt

def undo(g,args):
    s=require(g,args['task_id']);g.check_context(args['context_token']);return adoption.undo(g,s)
