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
        'recommended':m['recommended'],'note':m['note'],
        'risk_summary':risk_summary(m),
        'definitions':{'risk_count':'新增或加剧风险的商品条数；不是全部缺货风险',
            'recommended':'仅表示未发现新增风险或硬问题；不表示原有缺口消失，不授权自动采用',
            'parameters':'候选完整参数用stock_diagnosis_parameters，task_id与本次相同；不是effective_parameters的preview_id',
            'return':'门店回传，不是退货'},
        'page':page(m['details'],args.get('offset',0),args.get('limit',50))}


def risk_summary(comparison):
    """Count store/SKU observations, never sum quantities across different units."""
    rows=comparison['details']
    def counts(items):
        return {'before_gap_skus':sum(r['deficit_before'] is not None and r['deficit_before']>0 for r in items),
            'after_gap_skus':sum(r['deficit_after'] is not None and r['deficit_after']>0 for r in items),
            'unknown_gap_skus':sum(r['deficit_before'] is None or r['deficit_after'] is None for r in items),
            'arrival_gap_skus':sum(r['arrival_gap_without_timed_inbound'] is not None and r['arrival_gap_without_timed_inbound']>0 for r in items),
            'new_or_worsened_skus':sum(bool(r['risks']) for r in items),
            'reduced_gap_skus':sum(r['deficit_before'] is not None and r['deficit_after'] is not None and r['deficit_after']<r['deficit_before'] for r in items)}
    return {**counts(rows),'stores':[{'store':s['store'],**counts([r for r in rows if r['store']==s['store']])} for s in comparison['stores']],
        'hard_issue_count':len(comparison['hard_issues']),
        'note':'按门店＋商品计数；缺口以预算后执行量和固定预测日均计算。到货前风险未假定在途准时到货。不同商品缺口数量不相加。未新增风险不等于无风险。'}


def parameters(g,args):
    from automation_queries import parameter_result
    s=require(g,args['task_id']);n=args['candidate']-1
    if not 0<=n<len(s['options']):raise CommandError('INVALID_ARGUMENT','候选不存在')
    def read(prepared):
        value=parameter_result(g.host,{'stores':args['stores'],'scope':'preview','preview_id':args['task_id']},prepared)
        value.pop('preview_id',None);value['scope']='stock_diagnosis'
        return value
    return {'task_id':args['task_id'],'candidate':n+1,'session':s['id'],
        'stale':g.token()!=(s.get('adopted_token') if s['adopted'] else s['context_token']),
        'adopted':s['adopted'],'baseline':read(s['baseline']['prepared']),
        'candidate_parameters':read(s['options'][n]['prepared']),
        'changes':[c for c in s['options'][n]['changes'] if c['store'] in args['stores']],
        'note':'两份完整参数均来自同次诊断的冻结记录，沿用现有参数与来源解释；不是当前默认，不表示已经采用。'}

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
