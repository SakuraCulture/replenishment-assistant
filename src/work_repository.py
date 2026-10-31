"""Local transactional daily records and immutable full parameter presets."""
import copy,hashlib,json,sqlite3,zlib,uuid
from contextlib import closing
from datetime import datetime,date,timedelta,timezone
from pathlib import Path
import parameter_schema as schema
import storage_chunks as chunks
from version_info import MODEL_VERSION
from io_stages import stage,digest as digest_bytes,compress as compress_bytes

def now():return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec='seconds')
def today():return now()[:10]
def encode_default(value):
    if isinstance(value,(datetime,date)):return {'__workbook_datetime__':value.isoformat(),'date_only':not isinstance(value,datetime)}
    raise TypeError('无法保存的输入类型：'+type(value).__name__)
def decode_object(d):
    if set(d)=={'__workbook_datetime__','date_only'}:return (date if d['date_only'] else datetime).fromisoformat(d['__workbook_datetime__'])
    return d
def dumps(data):return json.dumps(data,ensure_ascii=False,sort_keys=True,allow_nan=False,default=encode_default)
def loads(data):return json.loads(data,object_hook=decode_object)
class Conflict(ValueError):pass

def summary(payload):
    states=payload.get('states',{});results=payload.get('results',{});ready=[s for s,st in states.items() if st['state']=='已完成']
    for s in ready:
        if s not in results or not results[s]:raise ValueError('已完成门店缺少结果：'+s)
    if ready and payload.get('input_dirty'):raise ValueError('输入已变化，不能把旧结果保存为已完成')
    if any(st['state'] in {'计算中','排队中'} for st in states.values()):raise ValueError('请等待任务结束后保存')
    return {'stores':{s:{'name':st['name'],'state':st['state']} for s,st in states.items()},'completed':len(ready),'amount':round(sum(results[s]['summary']['amount'] for s in ready),2) if ready else None,'model_version':payload['model_version'],'input_dirty':payload.get('input_dirty',True),'phase':'全部完成' if states and len(ready)==len(states) else '部分完成' if ready else '准备中 / 需重算'}

class Repository:
    def __init__(self,folder):
        self.path=Path(folder)/'workbench.sqlite3';self.path.parent.mkdir(parents=True,exist_ok=True)
        with closing(self.connect()) as db,db:
            db.executescript('CREATE TABLE IF NOT EXISTS jobs(day TEXT PRIMARY KEY, revision TEXT NOT NULL, digest TEXT NOT NULL, saved TEXT NOT NULL, summary TEXT NOT NULL, payload BLOB NOT NULL); CREATE TABLE IF NOT EXISTS presets(id TEXT NOT NULL, version INTEGER NOT NULL, name TEXT NOT NULL, config TEXT NOT NULL, archived INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(id,version)); CREATE TABLE IF NOT EXISTS stores(code TEXT PRIMARY KEY,data TEXT NOT NULL);')
    def connect(self):
        db=sqlite3.connect(self.path,timeout=10);db.execute('PRAGMA synchronous=FULL');return db
    def presets(self,all_versions=True):
        builtin={'id':'standard','version':1,'name':'标准补货','config':schema.standard(),'builtin':True}
        with closing(self.connect()) as db:rows=db.execute('SELECT id,version,name,config FROM presets WHERE archived=0 ORDER BY name,version DESC').fetchall()
        return [builtin]+[{'id':i,'version':v,'name':n,'config':loads(c),'builtin':False} for i,v,n,c in rows]
    def save_preset(self,name,config,preset_id=None):
        schema.validate(config);name=name.strip()
        if not name or len(name)>60:raise ValueError('方案名称须为1～60字')
        if preset_id=='standard':raise ValueError('内置标准方案只读，请复制另存')
        pid=preset_id or uuid.uuid4().hex
        with closing(self.connect()) as db,db:
            db.execute('BEGIN IMMEDIATE');v=db.execute('SELECT COALESCE(MAX(version),0)+1 FROM presets WHERE id=?',(pid,)).fetchone()[0]
            db.execute('INSERT INTO presets(id,version,name,config) VALUES(?,?,?,?)',(pid,v,name,dumps(config)))
        return {'id':pid,'version':v,'name':name,'config':copy.deepcopy(config),'builtin':False}
    def archive_preset(self,pid,version):
        if pid=='standard':raise ValueError('标准方案不可归档')
        with closing(self.connect()) as db,db:db.execute('UPDATE presets SET archived=1 WHERE id=? AND version=?',(pid,version))
    def stores(self):
        with closing(self.connect()) as db:return {k:loads(v) for k,v in db.execute('SELECT code,data FROM stores')}
    def save_stores(self,values,*,expected=None):
        for val in values.values():schema.validate(val['preset']['config'])
        with closing(self.connect()) as db,db:
            if expected is not None:
                db.execute('BEGIN IMMEDIATE')
                for code,old in expected.items():
                    row=db.execute('SELECT data FROM stores WHERE code=?',(code,)).fetchone()
                    if (loads(row[0]) if row else None)!=old:raise Conflict('门店默认在核对后变化，请刷新后重新核对：'+code)
            for key,val in values.items():db.execute('INSERT INTO stores VALUES(?,?) ON CONFLICT(code) DO UPDATE SET data=excluded.data',(key,dumps(val)))
    def list_jobs(self):
        with closing(self.connect()) as db:return [{'day':d,'revision':r,'digest':h,'saved':t,'summary':loads(s)} for d,r,h,t,s in db.execute('SELECT day,revision,digest,saved,summary FROM jobs ORDER BY day DESC')]
    def get_job(self,day,*,paths=None,progress=lambda _:None):
        with closing(self.connect()) as db:
            row=db.execute('SELECT revision,digest,saved,summary,payload FROM jobs WHERE day=?',(day,)).fetchone()
            if not row:return None
            payload=chunks.load(row[4],db,expected=row[1],paths=paths,progress=progress)
        return {'day':day,'revision':row[0],'digest':row[1],'saved':row[2],'summary':loads(row[3]),'payload':payload}
    def known_chunks(self):
        with closing(self.connect()) as db:return chunks.known(db)
    def review_job(self,day,progress=lambda _:None):
        paths=[('store_presets',),('store_ui',),('loaded','hashes'),('catalogs','*','revision'),('profile','revision')]
        record=self.get_job(day,paths=paths,progress=progress)
        if record is None:return None
        parts=record['payload'];record['payload']={'store_presets':parts.get(('store_presets',),{}),'store_ui':parts.get(('store_ui',),{}),'loaded':{'hashes':parts.get(('loaded','hashes'),{})},'catalogs':{k:{'revision':v} for k,v in parts.get(('catalogs','*','revision'),{}).items()},'profile':{'revision':parts.get(('profile','revision'))}}
        return record

    def save_job(self,day,payload,expected_revision=None):
        return self._save_prepared(day,prepare_job(day,payload,known=self.known_chunks()),expected_revision)

    def _save_prepared(self,day,prepared,expected_revision=None,progress=lambda _:None):
        # Internal session-only immutable encoded snapshot; never reused across calls.
        info,raw,digest=prepared;stamp=now()
        chunks.backup_before_write(self.path,progress)
        with stage('提交每日作业（提交期间不可取消）',progress),closing(self.connect()) as db,db:
            db.execute('BEGIN IMMEDIATE');old=db.execute('SELECT revision,digest,saved FROM jobs WHERE day=?',(day,)).fetchone()
            if old and expected_revision!=old[0]:raise Conflict('该日期已有记录或已被另一窗口更新，请重新查看差异并确认覆盖')
            if not old and expected_revision is not None:raise Conflict('原记录已发生变化，请重新保存')
            if old and digest==old[1]:return {'revision':old[0],'saved':old[2],'digest':digest}
            blob=chunks.put(db,raw) if isinstance(raw,chunks.Prepared) else compress_bytes(raw,progress)
            revision=uuid.uuid4().hex
            db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?) ON CONFLICT(day) DO UPDATE SET revision=excluded.revision,digest=excluded.digest,saved=excluded.saved,summary=excluded.summary,payload=excluded.payload',(day,revision,digest,stamp,dumps(info),blob))
        return {'revision':revision,'saved':stamp,'digest':digest}


def prepare_job(day,payload,progress=lambda _:None,*,known=()):
        if date.fromisoformat(day).isoformat()!=day:raise ValueError('日期须为YYYY-MM-DD')
        if payload.get('schema')!=1 or payload.get('day')!=day:raise ValueError('作业快照与日期不一致')
        info=summary(payload)
        prepared=chunks.prepare(payload,known,progress)
        return info,prepared,prepared.digest
