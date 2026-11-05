"""GUI-thread adoption transaction; never writes defaults or signs an order."""
import copy
from datetime import datetime
import getpass
import sqlite3
from contextlib import closing
from decimal import Decimal
import admission
from automation_protocol import CommandError,digest

ATTRS=('store_presets','overrides','_store_parameter_origins','states','results','active_plan','run_codes',
       '_calculation_input','_calculation_profile','_calculation_settings','_queued_retries','current_store','result','dirty','job_dirty')

def audit_values(value):
    # Preserve exact diagnostic decimals as explicit audit text; business results
    # keep their original numeric types and the existing storage codec is unchanged.
    if isinstance(value,Decimal):return str(value)
    if isinstance(value,dict):return {k:audit_values(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)):return [audit_values(v) for v in value]
    return value

def barrier(host):
    # Saving/signing must also prevent undo even when the UI context stays the same.
    with closing(sqlite3.connect(host.work_repository.path.resolve().as_uri()+'?mode=ro',uri=True)) as db:
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        values=[]
        if 'normal_orders' in tables:values.append(list(db.execute('SELECT oid,current FROM normal_orders WHERE day=? ORDER BY oid',(host.job_day,))))
        if 'jobs' in tables:values.append(list(db.execute('SELECT revision,digest,saved FROM jobs WHERE day=?',(host.job_day,))))
    return digest([host.saved_time,values])

def capture(h):
    return {'attrs':{k:getattr(h,k) for k in ATTRS if hasattr(h,k)},'ui':{s:copy.deepcopy(h.store_ui(s)) for s in h.store_widgets}}

def restore(h,state):
    h.initializing=True
    try:
        for k in ATTRS:
            if k in state['attrs']:setattr(h,k,state['attrs'][k])
            elif hasattr(h,k):delattr(h,k)
        for s,value in state['ui'].items():h.set_store_ui(s,value);h.refresh_parameter_label(s)
    finally:h.initializing=False
    h.selection_changed();h.refresh_tables();h.refresh_job_label()

def adopt(g,session,index,reason,accept_risks=False):
    g.idle();g.check_context(session['context_token']);h=g.host
    if session['adopted']:raise CommandError('ALREADY_ADOPTED','本次候选已采用，不重复执行')
    if not isinstance(index,int) or isinstance(index,bool) or not 0<=index<len(session['options']):raise ValueError('候选不存在')
    if not reason or not reason.strip():raise ValueError('请填写采用原因')
    option=session['options'][index];p=option['prepared'];r=option['calculation'];g.check_adoption(p)
    if option['comparison']['hard_issues']:raise CommandError('NEEDS_REVIEW','关键资料待核，不能采用')
    if option['comparison']['risks'] and not accept_risks:raise CommandError('NEEDS_REVIEW','候选增加保护期或最低持有缺口，须核对逐商品风险并明确确认')
    if list(r['results'])!=p['codes'] or any(r['states'][s]['state']!='已完成' for s in p['codes']):raise ValueError('候选不完整')
    for s in p['codes']:
        if admission.snapshot(h.work_repository.path,s)!=p['jobs'][s]['admission']:raise CommandError('ADMISSION_CHANGED','准入资料已变化：'+s)
    checkpoint=barrier(h);previous=capture(h)
    audit={'session':session['id'],'rule':session['rule'],'candidate':index+1,'changes':copy.deepcopy(option['changes']),
           'reason':reason.strip(),'risk_confirmed':bool(accept_risks),'risks':audit_values(option['comparison']['risks']),
           'diagnostic_decimal_encoding':'精确十进制文本，仅诊断审计，不改变业务数量金额类型',
           'baseline_hash':session['baseline_hash'],'candidate_hash':option['result_hash'],
           'at':datetime.now().astimezone().isoformat(),'operator':getpass.getuser(),'operator_kind':'本机登录用户名，非认证审批人'}
    try:
        # Copy maps before mutations so rollback retains the exact old result objects.
        for k in ('store_presets','overrides','_store_parameter_origins','states','results'):setattr(h,k,dict(getattr(h,k)))
        h.initializing=True
        for s in p['codes']:
            record=p['records'][s];h.store_presets[s]=copy.deepcopy(record['preset']);h.overrides[s]=copy.deepcopy(record['preset']['config']['warehouses'])
            h._store_parameter_origins.pop(s,None);h.set_store_ui(s,record['ui']);h.refresh_parameter_label(s)
            h.states[s]=copy.deepcopy(r['states'][s]);h.results[s]={**r['results'][s],'stock_diagnosis_audit':copy.deepcopy(audit)}
        h.run_codes=list(dict.fromkeys([*h.run_codes,*p['codes']]));h.reset_calculation_snapshot()
        h._calculation_input=h.loaded;h._calculation_profile=copy.deepcopy(p['profile']);h._calculation_settings=copy.deepcopy(p['jobs'])
        h.current_store=None;h.result=None;h.dirty=True;h.initializing=False
        h.selection_changed();h.refresh_tables();h.touch()
    except Exception:
        restore(h,previous);raise
    session.update(adopted=True,undo=previous,adopted_token=g.token(),barrier=checkpoint,audit=audit)
    return {'adopted':True,'context_token':g.token(),'message':'已采用本次参数及完整结果，尚未保存或签发；永久默认不变'}

def undo(g,session):
    g.idle();h=g.host
    if not session.get('adopted') or 'undo' not in session:raise ValueError('没有可撤回的采用')
    g.check_context(session['adopted_token'])
    if barrier(h)!=session['barrier']:raise CommandError('STALE_CONTEXT','作业已保存或签发，不能用撤回覆盖后续工作')
    current=capture(h)
    try:restore(h,session['undo'])
    except Exception:restore(h,current);raise
    session['undone']=True;session.pop('undo');return {'undone':True,'context_token':g.token(),'message':'已恢复采用前参数和结果，未改历史或默认'}
