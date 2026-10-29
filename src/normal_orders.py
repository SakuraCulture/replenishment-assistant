"""One current normal order per day/store, immutable revisions and atomic draft save."""
import copy,hashlib,json,sqlite3,uuid,zlib
from contextlib import closing
from datetime import date
from urllib.parse import quote
import work_repository as wr
import new_product_focus as focus
from version_info import MODEL_VERSION


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
def decode(value):return wr.loads(zlib.decompress(value))


def revision_diff(old,new):
    """Ordered added/removed and per-field changes, including non-quantity evidence."""
    before={d['E']:d for d in old['rows']} if old else {};after={d['E']:d for d in new['rows']}
    added=[{'sku':s,'name':after[s].get('D'),'candidate':after[s].get('_candidate_qty')} for s in after if s not in before]
    removed=[{'sku':s,'name':before[s].get('D'),'candidate':before[s].get('_candidate_qty')} for s in before if s not in after]
    changed=[]
    for s,d in after.items():
        if s not in before:continue
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

    def get(self,day,store,revision=None):
        with closing(self.work.connect()) as db:
            row=db.execute("SELECT oid,number,current FROM normal_orders WHERE day=? AND store=? AND kind='normal_model'",(day,store)).fetchone()
            if row is None:return None
            oid,number,current=row;rev=current if revision is None else revision
            data=db.execute('SELECT digest,saved,payload FROM normal_revisions WHERE oid=? AND revision=?',(oid,rev)).fetchone()
            if data is None:raise ValueError('归档修订不存在')
            payload=decode(data[2])
            if fingerprint_owned(payload['result'],payload.get('new_focus'))!=data[0]:raise ValueError('归档快照校验失败')
            return {'oid':oid,'number':number,'day':day,'store':store,'revision':rev,'current_revision':current,'digest':data[0],'saved':data[1],'payload':payload}

    def headers(self):
        with closing(self.work.connect()) as db:
            rows=db.execute("SELECT o.day,o.store,o.oid,o.number,o.current,r.digest,r.saved FROM normal_orders o JOIN normal_revisions r ON o.oid=r.oid AND o.current=r.revision ORDER BY o.day DESC,o.store").fetchall()
        return [dict(zip(('day','store','oid','number','revision','digest','saved'),r)) for r in rows]

    def list(self):
        with closing(self.work.connect()) as db:
            rows=db.execute("SELECT o.day,o.store,o.number,o.current,r.saved,r.payload FROM normal_orders o JOIN normal_revisions r ON o.oid=r.oid AND o.current=r.revision ORDER BY o.day DESC,o.store").fetchall()
        records=[]
        for day,store,number,rev,saved,p in rows:
            result=decode(p)['result']
            records.append({'day':day,'store':store,'number':number,'revision':rev,'saved':saved,'name':result['store_name'],'summary':result['summary']})
        return records

    def events(self,oid=None):
        with closing(self.work.connect()) as db:
            rows=db.execute('SELECT at,oid,revision,action,detail FROM normal_events'+(' WHERE oid=?' if oid else '')+' ORDER BY id DESC',(oid,) if oid else ()).fetchall()
        return [{'at':t,'oid':o,'revision':r,'action':a,'detail':wr.loads(d)} for t,o,r,a,d in rows]

    def log(self,order,action,detail):
        with closing(self.work.connect()) as db,db:
            db.execute('INSERT INTO normal_events(at,oid,revision,action,detail) VALUES(?,?,?,?,?)',(wr.now(),order['oid'],order['revision'],action,wr.dumps(detail)))

    def confirm(self,payload,codes,expected_job,expected_orders):
        day=payload['day'];date.fromisoformat(day)
        if day!=wr.today():raise ValueError('只能确认当前业务日期；历史只读，跨天请新建今日作业')
        if payload.get('schema')!=1 or payload.get('model_version')!=MODEL_VERSION:raise ValueError('作业或模型版本不兼容，请重新计算')
        info=wr.summary(payload)
        if not codes or len(codes)!=len(set(codes)):raise ValueError('请选择互不重复的门店')
        for s in codes:
            if payload['states'].get(s,{}).get('state')!='已完成' or payload.get('input_dirty'):raise ValueError('门店结果失效或未完成：'+s)
            r=payload['results'][s]
            from result_semantics import summarize
            if summarize(r['rows'])!=r['summary']:raise ValueError('门店汇总与明细不一致，请重新计算：'+s)
            if r.get('store')!=s or any(d.get('B')!=s for d in r['rows']):raise ValueError('门店结果混入其他门店')
            if len({d['E'] for d in r['rows']})!=len(r['rows']):raise ValueError('门店商品编码不唯一')
        focus.require_for(payload.get('new_focus'),day,[payload['results'][s] for s in codes])
        raw=wr.dumps(payload).encode();digest=hashlib.sha256(raw).hexdigest();stamp=wr.now();compressed=zlib.compress(raw)
        with closing(self.work.connect()) as db,db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT revision,digest,payload FROM jobs WHERE day=?',(day,)).fetchone()
            if (old[0] if old else None)!=expected_job:raise wr.Conflict('当天草稿已被另一窗口更新，请重新查看并确认')
            # Compare all expected revisions before any mutation.
            for s in codes:
                current=db.execute("SELECT current FROM normal_orders WHERE day=? AND store=? AND kind='normal_model'",(day,s)).fetchone()
                if (current[0] if current else None)!=expected_orders.get(s):raise wr.Conflict('正常单已更新，请重新查看差异：'+s)
            work_revision=old[0] if old else None
            if not old or old[1]!=digest:
                work_revision=uuid.uuid4().hex
                if old:db.execute('INSERT OR IGNORE INTO normal_work_revisions VALUES(?,?,?)',(old[0],day,old[2]))
                db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?) ON CONFLICT(day) DO UPDATE SET revision=excluded.revision,digest=excluded.digest,saved=excluded.saved,summary=excluded.summary,payload=excluded.payload',(day,work_revision,digest,stamp,wr.dumps(info),compressed))
            for s in codes:
                result=payload['results'][s];h=fingerprint(result,payload.get('new_focus'))
                current=db.execute("SELECT oid,number,current FROM normal_orders WHERE day=? AND store=? AND kind='normal_model'",(day,s)).fetchone()
                if current:
                    oid,number,prev=current
                    if db.execute('SELECT digest FROM normal_revisions WHERE oid=? AND revision=?',(oid,prev)).fetchone()[0]==h:continue
                    rev=prev+1
                else:
                    oid=str(uuid.uuid4());number='BH-'+day.replace('-','')+'-'+quote(s,safe='')+'-ZC';rev=1
                    db.execute('INSERT INTO normal_orders VALUES(?,?,?,?,?,?)',(day,s,'normal_model',oid,number,rev))
                record={'schema':1,'model_version':MODEL_VERSION,'work_revision':work_revision,'result':result,'preset':payload.get('store_presets',{}).get(s),'new_focus':copy.deepcopy(payload.get('new_focus')),'item_ids':{d['E']:uuid.uuid5(uuid.UUID(oid),d['E']).hex for d in result['rows']}}
                # Keep the issue's complete input/config draft even after later ordinary draft saves.
                db.execute('INSERT OR IGNORE INTO normal_work_revisions VALUES(?,?,?)',(work_revision,day,compressed))
                db.execute('INSERT INTO normal_revisions VALUES(?,?,?,?,?)',(oid,rev,h,stamp,encode(record)))
                db.execute('UPDATE normal_orders SET current=? WHERE oid=?',(rev,oid))
                db.execute('INSERT INTO normal_events(at,oid,revision,action,detail) VALUES(?,?,?,?,?)',(stamp,oid,rev,'确认归档' if rev==1 else '同日覆盖修订',wr.dumps({'number':number,'day':day,'store':s,'summary':result['summary'],'previous_revision':rev-1 or None})))
        return [self.get(day,s) for s in codes]
