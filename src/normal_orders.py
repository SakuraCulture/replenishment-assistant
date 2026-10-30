"""One current normal order per day/store, immutable revisions and atomic draft save."""
import copy,hashlib,json,sqlite3,uuid,zlib,time
from runtime_support import checkpoint,CommitProgress
from contextlib import closing
from datetime import date
from urllib.parse import quote
import work_repository as wr
import storage_chunks as chunks
import new_product_focus as focus
from version_info import MODEL_VERSION
from io_stages import stage,compress as compress_bytes
from export_progress import RowProgress,prefixed


def fingerprint(result,new_focus=None):
    business=copy.deepcopy(result)
    # Exact root-only nonbusiness whitelist. Original snapshot retains these fields.
    for key in ('created','task','version'):business.pop(key,None)
    if new_focus is not None:business={'result':business,'new_focus':focus.validate(new_focus)['revision']}
    return hashlib.sha256(wr.dumps(business).encode()).hexdigest()


def fingerprint_owned(result,new_focus=None):
    """Same canonical digest; caller exclusively owns the decoded, unmodified tree."""
    business={k:v for k,v in result.items() if k not in ('created','task','version')}
    if new_focus is not None:business={'result':business,'new_focus':focus.validate(new_focus)['revision']}
    return hashlib.sha256(wr.dumps(business).encode()).hexdigest()


def encode(value):return zlib.compress(wr.dumps(value).encode())
def decode(value,db=None):return chunks.load(value,db)


def revision_diff(old,new):
    """Ordered added/removed and per-field changes, including non-quantity evidence."""
    before={d['E']:d for d in old['rows']} if old else {};after={d['E']:d for d in new['rows']}
    added=[{'sku':s,'name':after[s].get('D'),'candidate':after[s].get('_candidate_qty')} for s in after if s not in before]
    removed=[{'sku':s,'name':before[s].get('D'),'candidate':before[s].get('_candidate_qty')} for s in before if s not in after]
    changed=[]
    for s,d in after.items():
        if s not in before:continue
        # Same canonical comparison as the field-by-field fallback, but serialize
        # an unchanged row twice instead of every one of its fields twice.
        # Plain dict equality would conflate 0/False/0.0 and is not equivalent.
        if wr.dumps(before[s])==wr.dumps(d):continue
        keys=list(before[s])+[k for k in d if k not in before[s]]
        fields={k:{'before_present':k in before[s],'after_present':k in d,'before':before[s].get(k),'after':d.get(k)} for k in keys if (k in before[s])!=(k in d) or wr.dumps(before[s].get(k))!=wr.dumps(d.get(k))}
        if fields:changed.append({'sku':s,'name':d.get('D'),'fields':fields})
    return {'added':added,'removed':removed,'changed':changed,'old_settings':old.get('effective_settings') if old else None,'new_settings':new.get('effective_settings'),'old_sources':old.get('sources') if old else None,'new_sources':new.get('sources')}


class Repository:
    def __init__(self, work):
        self.work=work
        with closing(work.connect()) as db:
            exists=db.execute("SELECT name FROM sqlite_master WHERE name='normal_orders'").fetchone()
            if not exists:
                # SQLite backup API also captures an existing WAL consistently.
                backup=work.path.with_name('workbench.before-normal-orders.'+uuid.uuid4().hex+'.sqlite3')
                with closing(sqlite3.connect(backup)) as target:db.backup(target)
            with db:db.executescript('''
                CREATE TABLE IF NOT EXISTS normal_orders(day TEXT NOT NULL,store TEXT NOT NULL,kind TEXT NOT NULL,oid TEXT NOT NULL UNIQUE,number TEXT NOT NULL UNIQUE,current INTEGER NOT NULL,PRIMARY KEY(day,store,kind));
                CREATE TABLE IF NOT EXISTS normal_revisions(oid TEXT NOT NULL,revision INTEGER NOT NULL,digest TEXT NOT NULL,saved TEXT NOT NULL,payload BLOB NOT NULL,PRIMARY KEY(oid,revision));
                CREATE TABLE IF NOT EXISTS normal_events(id INTEGER PRIMARY KEY AUTOINCREMENT,at TEXT NOT NULL,oid TEXT,revision INTEGER,action TEXT NOT NULL,detail TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS normal_work_revisions(revision TEXT PRIMARY KEY,day TEXT NOT NULL,payload BLOB NOT NULL);
            ''')

    def get(self,day,store,revision=None,*,view=None,progress=lambda _:None):
        with closing(self.work.connect()) as db:
            row=db.execute("SELECT oid,number,current FROM normal_orders WHERE day=? AND store=? AND kind='normal_model'",(day,store)).fetchone()
            if row is None:return None
            oid,number,current=row;rev=current if revision is None else revision
            data=db.execute('SELECT digest,saved,payload FROM normal_revisions WHERE oid=? AND revision=?',(oid,rev)).fetchone()
            if data is None:raise ValueError('归档修订不存在')
            fields={'export':[('result','rows'),('result','summary'),('result','store'),('result','store_name'),('new_focus',)],'review':[('result','summary'),('result','effective_settings'),('result','sources'),('new_focus',)]}
            if view is not None and view not in fields:raise ValueError('未知归档读取范围')
            if view and chunks.manifest(data[2]) is not None:
                parts=chunks.load(data[2],db,paths=fields[view],binding=data[0],progress=progress)
                payload={'result':{path[1]:value for path,value in parts.items() if path[0]=='result'},'new_focus':parts.get(('new_focus',))}
            else:
                payload=chunks.load(data[2],db,binding=data[0],progress=progress)
                if fingerprint_owned(payload['result'],payload.get('new_focus'))!=data[0]:raise ValueError('归档快照校验失败')
                if view:payload={'result':{path[1]:payload['result'][path[1]] for path in fields[view] if len(path)==2 and path[1] in payload['result']},'new_focus':payload.get('new_focus')}
            return {'oid':oid,'number':number,'day':day,'store':store,'revision':rev,'current_revision':current,'digest':data[0],'saved':data[1],'payload':payload}

    def head(self,day,store):
        with closing(self.work.connect()) as db:
            row=db.execute("SELECT o.day,o.store,o.oid,o.number,o.current,r.digest,r.saved FROM normal_orders o JOIN normal_revisions r ON o.oid=r.oid AND o.current=r.revision WHERE o.day=? AND o.store=? AND o.kind='normal_model'",(day,store)).fetchone()
        return dict(zip(('day','store','oid','number','revision','digest','saved'),row)) if row else None

    def headers(self):
        with closing(self.work.connect()) as db:
            rows=db.execute("SELECT o.day,o.store,o.oid,o.number,o.current,r.digest,r.saved FROM normal_orders o JOIN normal_revisions r ON o.oid=r.oid AND o.current=r.revision ORDER BY o.day DESC,o.store").fetchall()
        return [dict(zip(('day','store','oid','number','revision','digest','saved'),r)) for r in rows]

    def list(self):
        records=[]
        for head in self.headers():
            order=self.get(head['day'],head['store'],head['revision']);result=order['payload']['result']
            records.append({'day':head['day'],'store':head['store'],'number':head['number'],'revision':head['revision'],'saved':head['saved'],'name':result['store_name'],'summary':result['summary']})
        return records

    def events(self,oid=None):
        with closing(self.work.connect()) as db:
            rows=db.execute('SELECT at,oid,revision,action,detail FROM normal_events'+(' WHERE oid=?' if oid else '')+' ORDER BY id DESC',(oid,) if oid else ()).fetchall()
        return [{'at':t,'oid':o,'revision':r,'action':a,'detail':wr.loads(d)} for t,o,r,a,d in rows]

    def log(self,order,action,detail):
        with closing(self.work.connect()) as db,db:
            db.execute('INSERT INTO normal_events(at,oid,revision,action,detail) VALUES(?,?,?,?,?)',(wr.now(),order['oid'],order['revision'],action,wr.dumps(detail)))

    def confirm(self,payload,codes,expected_job,expected_orders,*,progress=lambda _:None,headers_only=False,cancel=None):
        notify=progress
        def progress(message):
            checkpoint(cancel);notify(message);checkpoint(cancel)
        checkpoint(cancel)
        day=payload['day'];date.fromisoformat(day)
        if day!=wr.today():raise ValueError('只能确认当前业务日期；历史只读，跨天请新建今日作业')
        if payload.get('schema')!=1 or payload.get('model_version')!=MODEL_VERSION:raise ValueError('作业或模型版本不兼容，请重新计算')
        info=wr.summary(payload)
        if not codes or len(codes)!=len(set(codes)):raise ValueError('请选择互不重复的门店')
        for s in codes:
            checkpoint(cancel)
            if payload['states'].get(s,{}).get('state')!='已完成' or payload.get('input_dirty'):raise ValueError('门店结果失效或未完成：'+s)
            r=payload['results'][s]
            from result_semantics import summarize
            if summarize(r['rows'])!=r['summary']:raise ValueError('门店汇总与明细不一致，请重新计算：'+s)
            if r.get('store')!=s or any(d.get('B')!=s for d in r['rows']):raise ValueError('门店结果混入其他门店')
            if len({d['E'] for d in r['rows']})!=len(r['rows']):raise ValueError('门店商品编码不唯一')
        focus.require_for(payload.get('new_focus'),day,[payload['results'][s] for s in codes])
        # Freeze preparation outside the write transaction. Compare every revision
        # again inside BEGIN IMMEDIATE before publishing any prepared content.
        with closing(self.work.connect()) as db:
            planned_job=db.execute('SELECT revision,digest FROM jobs WHERE day=?',(day,)).fetchone()
            planned={}
            for s in codes:
                planned[s]=db.execute("SELECT o.oid,o.number,o.current,r.digest,r.saved FROM normal_orders o JOIN normal_revisions r ON o.oid=r.oid AND o.current=r.revision WHERE o.day=? AND o.store=? AND o.kind='normal_model'",(day,s)).fetchone()
        existing=self.work.known_chunks()
        _,work_pack,digest=wr.prepare_job(day,payload,progress,known=existing);stamp=wr.now()
        existing.update(work_pack.chunks)
        changed_job=not planned_job or planned_job[1]!=digest
        work_revision=uuid.uuid4().hex if changed_job else planned_job[0]

        prepared=[];heads=[]
        for index,s in enumerate(codes,1):
            progress(RowProgress(f'准备正常单：{index-1} / {len(codes)} 家 · {s}',index-1,len(codes)))
            store_progress=prefixed(progress,f'门店 {index}/{len(codes)} · {s}')
            result=payload['results'][s];current=planned[s]
            with stage('校验门店业务指纹',store_progress):h=fingerprint_owned(result,payload.get('new_focus'))
            if current:
                oid,number,prev,old_digest,saved=current
            else:
                oid=str(uuid.uuid4());number='BH-'+day.replace('-','')+'-'+quote(s,safe='')+'-ZC';prev=0;saved=stamp
            same=bool(current and old_digest==h);rev=prev if same else prev+1
            heads.append({'oid':oid,'number':number,'day':day,'store':s,'revision':rev,'current_revision':rev,'digest':h,'saved':saved if same else stamp})
            if not same:
                record={'schema':1,'model_version':MODEL_VERSION,'work_revision':work_revision,'result':result,'preset':payload.get('store_presets',{}).get(s),'new_focus':copy.deepcopy(payload.get('new_focus')),'item_ids':{d['E']:uuid.uuid5(uuid.UUID(oid),d['E']).hex for d in result['rows']}}
                encoded=chunks.prepare(record,existing,store_progress,binding=h)
                existing.update(encoded.chunks)
                prepared.append((s,oid,number,rev,h,encoded,result['summary']))
            progress(RowProgress(f'准备正常单：{index} / {len(codes)} 家',index,len(codes)))
        if changed_job or prepared:chunks.backup_before_write(self.work.path,progress)
        checkpoint(cancel)
        with closing(self.work.connect()) as db,db:
            progress('等待归档写锁（可取消）…')
            db.execute('PRAGMA busy_timeout=100');deadline=time.monotonic()+10
            while True:
                checkpoint(cancel)
                try:db.execute('BEGIN IMMEDIATE');break
                except sqlite3.OperationalError as exc:
                    if 'locked' not in str(exc).lower() or time.monotonic()>=deadline:raise
            progress('已取得写锁，校验修订并准备事务内容（可取消）…')
            checkpoint(cancel)
            old=db.execute('SELECT revision,digest FROM jobs WHERE day=?',(day,)).fetchone()
            if (old[0] if old else None)!=expected_job:raise wr.Conflict('当天草稿已被另一窗口更新，请重新查看并确认')
            if old!=planned_job:raise wr.Conflict('当天草稿已被另一窗口更新，请重新查看并确认')
            # Compare all expected revisions before any mutation.
            for s in codes:
                current=db.execute("SELECT current FROM normal_orders WHERE day=? AND store=? AND kind='normal_model'",(day,s)).fetchone()
                if (current[0] if current else None)!=expected_orders.get(s):raise wr.Conflict('正常单已更新，请重新查看差异：'+s)
                if (current[0] if current else None)!=(planned[s][2] if planned[s] else None):raise wr.Conflict('正常单已更新，请重新查看差异：'+s)
            if changed_job or prepared:compressed=chunks.put(db,work_pack,cancel=cancel)
            if changed_job:
                if old:db.execute('INSERT OR IGNORE INTO normal_work_revisions SELECT revision,day,payload FROM jobs WHERE day=?',(day,))
                db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?) ON CONFLICT(day) DO UPDATE SET revision=excluded.revision,digest=excluded.digest,saved=excluded.saved,summary=excluded.summary,payload=excluded.payload',(day,work_revision,digest,stamp,wr.dumps(info),compressed))
            for s,oid,number,rev,h,encoded,summary in prepared:
                checkpoint(cancel)
                if not planned[s]:
                    db.execute('INSERT INTO normal_orders VALUES(?,?,?,?,?,?)',(day,s,'normal_model',oid,number,rev))
                # Keep the issue's complete input/config draft even after later ordinary draft saves.
                db.execute('INSERT OR IGNORE INTO normal_work_revisions SELECT revision,day,payload FROM jobs WHERE day=?',(day,))
                db.execute('INSERT INTO normal_revisions VALUES(?,?,?,?,?)',(oid,rev,h,stamp,chunks.put(db,encoded,cancel=cancel)))
                db.execute('UPDATE normal_orders SET current=? WHERE oid=?',(rev,oid))
                db.execute('INSERT INTO normal_events(at,oid,revision,action,detail) VALUES(?,?,?,?,?)',(stamp,oid,rev,'确认归档' if rev==1 else '同日覆盖修订',wr.dumps({'number':number,'day':day,'store':s,'summary':summary,'previous_revision':rev-1 or None})))
            progress('事务内容准备完成，检查最终取消请求…')
            checkpoint(cancel)
            # Linearization boundary: after this check, commit must finish or fail.
            # Never interrupt SQLite COMMIT or kill its process.
            notify(CommitProgress(True))
            db.commit()
        notify(CommitProgress(False))
        if headers_only:return heads
        with stage('读取并验证签发快照',notify):return [self.get(day,h['store'],h['revision'],progress=notify) for h in heads]
