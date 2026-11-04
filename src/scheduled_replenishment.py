"""Durable schedules and isolated calculation results. No Qt or platform actions."""
import copy, hashlib, json, sqlite3, uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone, date, time
from pathlib import Path
import work_repository as wr
import storage_chunks as chunks

TZ=timezone(timedelta(hours=8))
ACTIVE={'QUEUED','RUNNING','PAUSING'}
TERMINAL={'COMPLETE','ENDED','MISSED'}
STATES={'QUEUED':'排队','RUNNING':'执行中','PAUSING':'正在安全暂停','PAUSED':'已暂停','REVIEW':'需处理','COMPLETE':'计算完成，等待核对','ENDED':'已结束','MISSED':'错过执行时间'}

def stamp():return datetime.now(TZ)
def signature(value):return hashlib.sha256(wr.dumps(value).encode()).hexdigest()
def schedule_value(value):
    x=copy.deepcopy(value)
    if not isinstance(x.get('plan_id'),str) or not x['plan_id']:raise ValueError('请选择补货预案')
    if x.get('kind') not in ('weekly','once'):raise ValueError('请选择重复方式')
    try:
        t=time.fromisoformat(x['time'])
        if t.tzinfo or t.second or t.microsecond or len(x['time'])!=5:raise ValueError()
    except (KeyError,TypeError,ValueError):raise ValueError('开始时间须为 HH:mm') from None
    if x['kind']=='weekly':
        days=x.get('weekdays')
        if not isinstance(days,list) or not days or any(type(d) is not int or d not in range(7) for d in days) or len(set(days))!=len(days):raise ValueError('请勾选执行星期，不可重复')
    else:
        if date.fromisoformat(x.get('date','')).isoformat()!=x['date']:raise ValueError('执行日期无效')
    if type(x.get('workers',2)) is not int or not 1<=x.get('workers',2)<=4:raise ValueError('同时计算门店数须为1至4')
    x['workers']=x.get('workers',2);x['timezone']='Asia/Shanghai'
    x['name']=str(x.get('name','')).strip()
    if not x['name'] or len(x['name'])>80:raise ValueError('安排名称须为1至80字')
    if type(x.get('enabled',False)) is not bool:raise ValueError('启用状态无效')
    return x

def next_at(s,after):
    after=after.astimezone(TZ)
    for offset in range(9):
        day=date.fromisoformat(s['date']) if s['kind']=='once' else after.date()+timedelta(days=offset)
        dt=datetime.combine(day,time.fromisoformat(s['time']),TZ)
        if dt>after and (s['kind']=='once' or day.weekday() in s['weekdays']):return dt.isoformat()
        if s['kind']=='once':break
    return None

class Repository:
    def __init__(self,path,clock=stamp):self.path=Path(path);self.clock=clock

    @contextmanager
    def db(self,write=False):
        db=sqlite3.connect(self.path,timeout=1);db.row_factory=sqlite3.Row
        try:
            if write:db.execute('BEGIN IMMEDIATE')
            yield db
            if write:db.commit()
        except BaseException:db.rollback();raise
        finally:db.close()

    def exists(self):
        with self.db() as db:return bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='scheduled_runs'").fetchone())

    def ensure(self):
        if self.exists():return
        chunks.backup_before_write(self.path,lambda _:None)
        with self.db(True) as db:
            db.execute('CREATE TABLE IF NOT EXISTS scheduled_arrangements(id TEXT PRIMARY KEY,payload TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS scheduled_runs(id TEXT PRIMARY KEY,trigger_key TEXT UNIQUE NOT NULL,day TEXT NOT NULL,state TEXT NOT NULL,payload TEXT NOT NULL)')
            db.execute('CREATE INDEX IF NOT EXISTS scheduled_runs_day ON scheduled_runs(day,state)')
            db.execute('CREATE TABLE IF NOT EXISTS scheduled_values(run_id TEXT NOT NULL,name TEXT NOT NULL,digest TEXT NOT NULL,payload BLOB NOT NULL,PRIMARY KEY(run_id,name))')
            db.execute('CREATE TABLE IF NOT EXISTS scheduled_events(run_id TEXT NOT NULL,seq INTEGER NOT NULL,at TEXT NOT NULL,message TEXT NOT NULL,PRIMARY KEY(run_id,seq))')

    def schedules(self):
        if not self.exists():return []
        with self.db() as db:return [wr.loads(r[0]) for r in db.execute('SELECT payload FROM scheduled_arrangements ORDER BY rowid')]

    def save_schedule(self,value,expected=None):
        self.ensure();v=schedule_value(value);now=self.clock()
        v.update(id=expected['id'] if expected else uuid.uuid4().hex,revision=expected['revision']+1 if expected else 1,updated=now.isoformat())
        if v['enabled'] and not v.get('check_signature'):raise ValueError('请先检查运行条件，或仅保存暂不启用')
        v['next_at']=next_at(v,now) if v['enabled'] else None
        if v['enabled'] and not v['next_at']:raise ValueError('执行时间已过去，请选择下一次时间')
        with self.db(True) as db:
            old=db.execute('SELECT payload FROM scheduled_arrangements WHERE id=?',(v['id'],)).fetchone()
            if expected and (not old or wr.loads(old[0])!=expected):raise wr.Conflict('安排已变化，请刷新后编辑')
            db.execute('INSERT INTO scheduled_arrangements VALUES(?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload',(v['id'],wr.dumps(v)))
        return v

    def delete_schedule(self,expected):
        with self.db(True) as db:
            r=db.execute('SELECT payload FROM scheduled_arrangements WHERE id=?',(expected['id'],)).fetchone()
            if not r or wr.loads(r[0])!=expected:raise wr.Conflict('安排已变化，请刷新')
            db.execute('DELETE FROM scheduled_arrangements WHERE id=?',(expected['id'],))

    def _get(self,db,rid):
        r=db.execute('SELECT payload FROM scheduled_runs WHERE id=?',(rid,)).fetchone()
        if not r:raise ValueError('原任务已不存在')
        return wr.loads(r[0])

    def get(self,rid):
        with self.db() as db:return self._get(db,rid)

    def _save(self,db,r,message=None):
        r['revision']+=1;r['updated']=self.clock().isoformat()
        if message is not None:
            r['message']=str(message);r['event_seq']=r.get('event_seq',0)+1
            db.execute('INSERT INTO scheduled_events VALUES(?,?,?,?)',(r['id'],r['event_seq'],r['updated'],str(message)))
        db.execute('UPDATE scheduled_runs SET state=?,payload=? WHERE id=?',(r['state'],wr.dumps(r),r['id']))
        return r

    def _create(self,db,s,key,when,missed=False):
        old=db.execute('SELECT payload FROM scheduled_runs WHERE trigger_key=?',(key,)).fetchone()
        if old:return wr.loads(old[0])
        r={'id':uuid.uuid4().hex,'schedule':copy.deepcopy(s),'trigger_key':key,'day':when.astimezone(TZ).date().isoformat(),
           'trigger':'manual' if key.startswith('manual:') else 'timer','created':self.clock().isoformat(),'updated':self.clock().isoformat(),
           'state':'MISSED' if missed else 'QUEUED','stage':'检查运行条件','revision':0,'epoch':0,'event_seq':0,'stores':[],
           'warehouses':{},'message':'超过开始时间，未自动补跑' if missed else '等待执行','viewed':False,'control':None,'external':{}}
        db.execute('INSERT INTO scheduled_runs VALUES(?,?,?,?,?)',(r['id'],key,r['day'],r['state'],wr.dumps(r)))
        return self._save(db,r,r['message'])

    def manual(self,s):
        self.ensure();s=schedule_value(s)
        with self.db(True) as db:return self._create(db,s,'manual:'+uuid.uuid4().hex,self.clock())

    def due(self):
        if not self.exists():return []
        now=self.clock();out=[]
        with self.db(True) as db:
            for row in db.execute('SELECT payload FROM scheduled_arrangements ORDER BY rowid').fetchall():
                s=wr.loads(row[0]);dt=datetime.fromisoformat(s['next_at']) if s.get('next_at') else None
                if not s['enabled'] or not dt or dt>now:continue
                out.append(self._create(db,s,s['id']+':'+dt.isoformat(),dt,(now-dt).total_seconds()>60))
                s['next_at']=next_at(s,now);s['enabled']=bool(s['next_at']);s['revision']+=1
                db.execute('UPDATE scheduled_arrangements SET payload=? WHERE id=?',(wr.dumps(s),s['id']))
        return out

    def list(self,*,offset=0,limit=50,query='',group='all',start=None,end=None):
        if type(limit) is not int or not 1<=limit<=100 or type(offset) is not int or offset<0:raise ValueError('分页范围无效')
        if not self.exists():return {'items':[],'total':0,'next_offset':None}
        where=[];args=[]
        if query:where.append('instr(payload,?)>0');args.append(query)
        if start:where.append("(day>=? OR state NOT IN ('ENDED','MISSED') AND json_extract(payload,'$.viewed')=0)");args.append(start)
        if end:where.append('day<=?');args.append(end)
        groups={'active':"state IN ('QUEUED','RUNNING','PAUSING','PAUSED')",'review':"state IN ('REVIEW','MISSED')",'ready':"EXISTS(SELECT 1 FROM json_each(payload,'$.stores') WHERE json_extract(value,'$.state')='READY')",'ended':"state='ENDED'"}
        if group in groups:where.append(groups[group])
        elif group!='all':raise ValueError('状态筛选无效')
        condition=' WHERE '+' AND '.join('('+w+')' for w in where) if where else ''
        with self.db() as db:
            total=db.execute('SELECT COUNT(*) FROM scheduled_runs'+condition,args).fetchone()[0]
            rows=db.execute('SELECT payload FROM scheduled_runs'+condition+' ORDER BY rowid DESC LIMIT ? OFFSET ?',args+[limit,offset]).fetchall()
        return {'items':[wr.loads(x[0]) for x in rows],'total':total,'next_offset':offset+limit if offset+limit<total else None}

    def next_queued(self):
        if not self.exists():return None
        with self.db() as db:
            r=db.execute("SELECT payload FROM scheduled_runs WHERE state='QUEUED' ORDER BY rowid LIMIT 1").fetchone()
            return wr.loads(r[0]) if r else None

    def change(self,rid,*,epoch=None,expected=None,message=None,**fields):
        with self.db(True) as db:
            r=self._get(db,rid)
            if epoch is not None and r['epoch']!=epoch:raise wr.Conflict('原执行轮次已失效')
            if expected is not None and r['revision']!=expected:raise wr.Conflict('任务状态已变化，请刷新')
            r.update(copy.deepcopy(fields));return self._save(db,r,message)

    def claim(self,rid):
        with self.db(True) as db:
            r=self._get(db,rid)
            if r['state']!='QUEUED':raise wr.Conflict('任务已经由其他执行器处理')
            r.update(state='RUNNING',epoch=r['epoch']+1,control=None)
            return self._save(db,r,'正在检查原任务及冻结依据')

    def request_pause(self,rid,end=False):
        with self.db(True) as db:
            r=self._get(db,rid)
            if r['state'] in TERMINAL and not (end and r['state']=='COMPLETE'):raise ValueError('本次任务已经结束')
            running=r['state'] in ('RUNNING','PAUSING');r['control']='end' if end else 'pause'
            r['state']='PAUSING' if running else 'ENDED' if end else 'PAUSED'
            return self._save(db,r,'正在安全结束' if end and running else '正在安全暂停' if running else STATES[r['state']])

    def resume(self,rid,expected,codes=None):
        with self.db(True) as db:
            r=self._get(db,rid)
            if r['revision']!=expected or r['state'] not in ('REVIEW','PAUSED'):raise wr.Conflict('只能继续已暂停或需处理的原任务，请刷新')
            failures=[s['code'] for s in r['stores'] if s['state']!='READY']
            if codes is not None and (not codes or len(set(codes))!=len(codes) or set(codes)-set(failures)):raise ValueError('只能重试本任务未完成门店，成功门店保持')
            r.update(state='QUEUED',control=None,retry_codes=codes,viewed=False)
            return self._save(db,r,'已排入原任务；成功项保持，不更换资料')

    def recover(self):
        if not self.exists():return
        with self.db(True) as db:
            for row in db.execute("SELECT payload FROM scheduled_runs WHERE state IN ('RUNNING','PAUSING')").fetchall():
                r=wr.loads(row[0]);r.update(state='ENDED' if r.get('control')=='end' else 'PAUSED',epoch=r['epoch']+1)
                self._save(db,r,'上次中断；继续前核对原平台任务，不重新提交')

    def save_value(self,rid,name,value,epoch,*,store=None):
        # Encode outside the write transaction; immutable blocks deduplicate shared data.
        with self.db() as db:known=chunks.known(db)
        p=chunks.prepare(value,known)
        with self.db(True) as db:
            r=self._get(db,rid)
            if r['epoch']!=epoch or r['state'] not in ('RUNNING','PAUSING'):raise wr.Conflict('本次执行已退出，未采用迟到结果')
            blob=chunks.put(db,p)
            db.execute('INSERT INTO scheduled_values VALUES(?,?,?,?) ON CONFLICT(run_id,name) DO UPDATE SET digest=excluded.digest,payload=excluded.payload',(rid,name,p.digest,blob))
            if store:
                item=next(i for i in r['stores'] if i['code']==store)
                item.update(state='READY',message='已计算并保存，尚未签发',amount=value['summary']['amount'])
                self._save(db,r, item['name']+'计算结果已保存')

    def value(self,rid,name,default=None):
        with self.db() as db:
            row=db.execute('SELECT digest,payload FROM scheduled_values WHERE run_id=? AND name=?',(rid,name)).fetchone()
            return chunks.load(row['payload'],db,expected=row['digest']) if row else default

    def store_state(self,rid,code,state,message,epoch):
        with self.db(True) as db:
            r=self._get(db,rid)
            if r['epoch']!=epoch:raise wr.Conflict('本次执行轮次已过期')
            item=next(i for i in r['stores'] if i['code']==code)
            if item['state']=='READY':return r
            item.update(state=state,message=str(message));return self._save(db,r)

    def events(self,rid,after=0,limit=100):
        if not 1<=limit<=100:raise ValueError('每页最多100条')
        with self.db() as db:return [dict(x) for x in db.execute('SELECT seq,at,message FROM scheduled_events WHERE run_id=? AND seq>? ORDER BY seq LIMIT ?',(rid,after,limit))]

    def delete_run(self,rid,expected):
        with self.db(True) as db:
            r=self._get(db,rid)
            if r['revision']!=expected or r['state'] not in ('ENDED','MISSED'):raise wr.Conflict('请先结束本次任务并刷新，再删除记录')
            for table in ('scheduled_values','scheduled_events'):db.execute('DELETE FROM '+table+' WHERE run_id=?',(rid,))
            db.execute('DELETE FROM scheduled_runs WHERE id=?',(rid,))
            # Shared chunks and source originals are collected only by the existing reference-aware maintenance.

class SchedulerLock:
    """Process lifetime lock, independent of business transactions and browser leases."""
    def __init__(self,folder):self.path=Path(folder)/'scheduled_replenishment.lock.sqlite3';self.db=None
    def acquire(self):
        if self.db:return True
        db=sqlite3.connect(self.path,timeout=0,check_same_thread=False)
        try:db.execute('CREATE TABLE IF NOT EXISTS owner(id INTEGER)');db.execute('BEGIN IMMEDIATE')
        except sqlite3.OperationalError:db.close();return False
        self.db=db;return True
    def close(self):
        if self.db:self.db.close();self.db=None
