"""Versioned policy targets and opening assortments in the workbench database."""
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import json
import uuid

import work_repository as wr
from app_defaults import WAREHOUSES
from jst_allocation import number, text
from jst_order_store import digest

BEIJING = timezone(timedelta(hours=8))
KINDS = {'policy': '政策订单', 'opening': '新店开业'}
MODES = ('固定新增', '持有下限', '任选款', '门店确认量')
SCOPES = ('store', 'warehouse', 'account', 'shop', 'merchant', 'supplier', 'currency', 'settlement')


def now():
    return datetime.now(BEIJING).isoformat(timespec='seconds')


def instant(value):
    stamp = datetime.fromisoformat(value)
    return (stamp if stamp.tzinfo else stamp.replace(tzinfo=BEIJING)).astimezone(BEIJING)


def uid(*parts):
    return uuid.uuid5(uuid.NAMESPACE_URL, wr.dumps(list(parts))).hex


def required(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(label+'不能为空，编码请用文本')
    return value.strip()


def schema(db):
    definitions = {
        'special_plans': 'plan TEXT PRIMARY KEY,kind TEXT NOT NULL,version INTEGER NOT NULL,title TEXT NOT NULL',
        'special_versions': 'plan TEXT,version INTEGER,at TEXT,actor TEXT,reason TEXT,sha TEXT,body TEXT,PRIMARY KEY(plan,version)',
        'special_confirmations': 'plan TEXT,version INTEGER,store TEXT,seq INTEGER,sha TEXT,body TEXT,at TEXT,actor TEXT,PRIMARY KEY(plan,version,store,seq)',
        'special_observations': 'plan TEXT,store TEXT,sku TEXT,snapshot TEXT,at TEXT,sha TEXT,body TEXT,PRIMARY KEY(plan,store,sku,snapshot)',
        'special_runs': 'run TEXT PRIMARY KEY,plan TEXT,version INTEGER,wave INTEGER,stamp TEXT,sha TEXT,body TEXT,UNIQUE(plan,wave)',
        'special_packages': 'oid TEXT PRIMARY KEY,plan TEXT,run TEXT,position INTEGER,gid TEXT UNIQUE,scope TEXT,number TEXT UNIQUE,online TEXT UNIQUE,body TEXT,UNIQUE(run,position)',
        'special_claims': 'plan TEXT,line TEXT,store TEXT,warehouse TEXT,sku TEXT,oid TEXT,gid TEXT,qty TEXT,PRIMARY KEY(gid,line)',
        'special_releases': 'gid TEXT,warehouse TEXT,sku TEXT,qty TEXT,source TEXT,PRIMARY KEY(gid,sku)',
        'special_settings': 'kind TEXT PRIMARY KEY,enabled INTEGER NOT NULL',
        'special_tickets': 'plan TEXT,wave INTEGER,sha TEXT,body TEXT,state TEXT,message TEXT,PRIMARY KEY(plan,wave)',
        'special_records': 'object TEXT,stage TEXT,version INTEGER,sha TEXT,at TEXT,actor TEXT,body TEXT,PRIMARY KEY(object,stage,version)',
        'special_groups': 'id TEXT PRIMARY KEY,plan TEXT,scope TEXT,anchor TEXT,UNIQUE(plan,scope)',
        'special_receipts': 'merchant TEXT,store TEXT,original TEXT,group_id TEXT UNIQUE,version INTEGER,sha TEXT,PRIMARY KEY(merchant,store,original)',
        'special_effects': 'object TEXT,stage TEXT,state TEXT,token TEXT,sha TEXT,original TEXT,body TEXT,PRIMARY KEY(object,stage)',
    }
    for name, columns in definitions.items():
        db.execute(f'CREATE TABLE IF NOT EXISTS {name}({columns})')
    db.execute('CREATE INDEX IF NOT EXISTS special_claim_target ON special_claims(plan,line)')
    db.execute('CREATE INDEX IF NOT EXISTS special_run_plan ON special_packages(plan,run,position)')


@contextmanager
def connection(path, write=False):
    with closing(wr.Repository.open_existing(path).connect() if write else wr.read_connection(path)) as db, db:
        db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
        if write:
            schema(db)
        yield db


def exists(db, table='special_plans'):
    return bool(db.execute('SELECT 1 FROM sqlite_master WHERE name=?', (table,)).fetchone())


def enabled(db, kind):
    return exists(db,'special_settings') and db.execute('SELECT enabled FROM special_settings WHERE kind=?',(kind,)).fetchone()==(1,)


def set_enabled(path,kind,value):
    if kind not in KINDS:
        raise ValueError('未知业务类型')
    with connection(path,True) as db:
        db.execute('INSERT INTO special_settings VALUES(?,?) ON CONFLICT(kind) DO UPDATE SET enabled=excluded.enabled',(kind,int(bool(value))))


def all_plans(path,kind):
    with connection(path) as db:
        if not exists(db):
            return []
        return list(db.execute('SELECT plan,title,version FROM special_plans WHERE kind=? ORDER BY rowid DESC',(kind,)))


def read(db, plan, version=None):
    if version is None:
        row=db.execute('SELECT version FROM special_plans WHERE plan=?',(plan,)).fetchone()
        if not row:
            raise ValueError('请选择已有主单')
        version=row[0]
    sha,raw=db.execute('SELECT sha,body FROM special_versions WHERE plan=? AND version=?',(plan,version)).fetchone()
    body=json.loads(raw)
    if digest(body)!=sha:
        raise ValueError('主单修订摘要不符')
    return body


def record(db,obj,stage,body,actor,expected=None):
    previous=latest(db,obj,stage)
    sha=digest(body)
    if previous and previous['sha']==sha:
        return previous
    version=previous['version'] if previous else 0
    if expected is not None and expected!=version:
        raise wr.Conflict('该原单的当前版本已变更，请刷新')
    db.execute('INSERT INTO special_records VALUES(?,?,?,?,?,?,?)',(obj,stage,version+1,sha,now(),required(actor,'经办人'),wr.dumps(body)))
    return dict(version=version+1,sha=sha,body=body)


def latest(db,obj,stage,version=None):
    if not exists(db,'special_records'):
        return None
    sql='SELECT version,sha,body FROM special_records WHERE object=? AND stage=?'
    args=[obj,stage]
    if version is not None:
        sql+=' AND version=?'
        args.append(version)
    row=db.execute(sql+' ORDER BY version DESC LIMIT 1',args).fetchone()
    if row:
        data=json.loads(row[2])
        if digest(data)!=row[1]:
            raise ValueError('原单依据摘要不符')
        return dict(version=row[0],sha=row[1],body=data)


def scope(line):
    return {k:line[k] for k in SCOPES}


def procurement_scope(line):
    return {k:line[k] for k in ('store','warehouse','merchant','supplier','currency','settlement')}


def normalize(raw):
    kind=raw['kind']
    if kind not in KINDS or raw.get('mode','固定新增') not in MODES:
        raise ValueError('专项类型或目标模式无效')
    mode=raw.get('mode','固定新增')
    if kind=='opening' and mode!='固定新增':
        raise ValueError('开业货盘按冻结数量履约')
    start,end=instant(raw['start']),instant(raw['end'])
    if start>=end:
        raise ValueError('截止须晚于开始')
    strategy=raw.get('strategy','覆盖优先')
    if strategy not in ('覆盖优先','优先配齐'):
        raise ValueError('请选择库存分配顺序')
    stores=list(dict.fromkeys(required(s,'门店') for s in raw['stores']))
    if raw.get('store_type'):
        stores=[s for s in stores if raw.get('store_types',{}).get(s)==raw['store_type']]
    if raw.get('store_count') is not None:
        n=raw['store_count']
        if type(n) is not int or not 1<=n<=len(stores):
            raise ValueError('符合条件的门店数量不足或指定店数无效')
        stores=stores[:n]
    if not stores or kind=='opening' and len(stores)!=1:
        raise ValueError('开业主单须选一家店，政策须至少一家店')
    lines=[]
    seen=set()
    for pos,item in enumerate(raw['lines'],1):
        targets=[item['store']] if item.get('store') else stores
        for store in targets:
            if store not in stores:
                continue
            line={**raw.get('scope',{}),**item,'store':store}
            line.update(currency=line.get('currency','CNY'),settlement=line.get('settlement','人民币现结'))
            for key in (*SCOPES,'sku','unit'):
                line[key]=required(line.get(key),key)
            if line['warehouse'] not in WAREHOUSES or line['currency']!='CNY':
                raise ValueError('仓或币种与当前采购模板不兼容')
            line['source_row']=int(line.get('source_row',pos))
            line['id']=str(line.get('id') or uid(store,line['source_row']))
            if line['id'] in seen:
                raise ValueError('同一目标原行标识重复')
            seen.add(line['id'])
            line['target']=text(number(line['target'],'目标数量'))
            if 'start' in line or 'end' in line:
                if max(start,instant(line.get('start',raw['start'])))>=min(end,instant(line.get('end',raw['end']))):
                    raise ValueError('原行时段与主单没有交集')
            if line.get('round_up') and not line.get('round_approval'):
                raise ValueError('整箱上浮须有预先批准依据')
            lines.append(line)
    if not lines:
        raise ValueError('门店范围内没有货盘或目标行')
    k=int(raw.get('k',1))
    if mode=='任选款' and any(k<1 or k>len({r['sku'] for r in lines if r['store']==s}) for s in stores):
        raise ValueError('所需款数超过门店可选清单')
    if kind=='opening' and raw.get('single_purchase',True) and len({wr.dumps(procurement_scope(r)) for r in lines})>1:
        raise ValueError('整店一张采购与供货范围冲突，请核对收货店、商家、供货仓、供应商及结算条件')
    if raw.get('quota') is not None:
        total=sum(number(r['target'],'目标') for r in lines)
        if total>number(raw['quota'],'总配额'):
            raise ValueError('超出政策总配额 '+text(total-number(raw['quota'],'总配额'))+' 件')
    return dict(kind=kind,title=required(raw.get('title'),'主单名称'),mode=mode,stores=stores,lines=lines,
                start=start.isoformat(),end=end.isoformat(),strategy=strategy,k=k,maintain=bool(raw.get('maintain')),
                require_confirmation=bool(raw.get('require_confirmation') or mode=='门店确认量'),
                scope_filter={k:raw[k] for k in ('store_type','store_count','store_types','quota') if k in raw},
                single_purchase=bool(raw.get('single_purchase',True)),sequence=stores)


def publish(path,raw,actor,*,plan=None,expected=0,reason='首次发布'):
    body=normalize(raw)
    required(actor,'批准人')
    required(reason,'发布或修订依据')
    plan=plan or uuid.uuid4().hex
    with connection(path,True) as db:
        old=db.execute('SELECT kind,version FROM special_plans WHERE plan=?',(plan,)).fetchone()
        if (old[1] if old else 0)!=expected or old and old[0]!=body['kind']:
            raise wr.Conflict('原主单已修订，请重读原版本')
        if old:
            previous=read(db,plan)
            old_lines={r['id']:r for r in previous['lines']}
            if set(old_lines)-{r['id'] for r in body['lines']}:
                raise ValueError('原行必须保留，免供请以原行数量零并记录批准原因')
            for line in body['lines']:
                origin=old_lines.get(line['id'])
                if origin and any(origin[key]!=line[key] for key in ('store','sku','warehouse','unit',*SCOPES)):
                    raise ValueError('替代商品须追加新行并标注替代的原行，原行须保留')
        version=expected+1
        body.update(plan=plan,version=version)
        db.execute('INSERT INTO special_plans VALUES(?,?,?,?) ON CONFLICT(plan) DO UPDATE SET version=excluded.version,title=excluded.title',
                   (plan,body['kind'],version,body['title']))
        db.execute('INSERT INTO special_versions VALUES(?,?,?,?,?,?,?)',(plan,version,now(),actor,reason,digest(body),wr.dumps(body)))
    return plan


def confirm(path,plan,version,store,quantities,actor):
    required(actor,'确认人')
    with connection(path,True) as db:
        body=read(db,plan)
        if version!=body['version'] or store not in body['stores']:
            raise wr.Conflict('门店或回复版本与当前发布范围不符')
        eligible={r['id']:r for r in body['lines'] if r['store']==store}
        values={k:text(number(v,'确认数量')) for k,v in quantities.items()}
        if values.keys()-eligible.keys() or any(Decimal(v)>Decimal(eligible[k]['target']) for k,v in values.items()):
            raise ValueError('门店回复超出冻结的原行或目标量')
        positive={eligible[k]['sku'] for k,v in values.items() if Decimal(v)>0}
        if positive and body['mode']=='任选款' and len(positive)!=body['k']:
            raise ValueError('请按发布的不同 SKU 款数确认，或明确回复零')
        sha=digest(values)
        last=db.execute('SELECT seq,sha FROM special_confirmations WHERE plan=? AND version=? AND store=? ORDER BY seq DESC LIMIT 1',(plan,version,store)).fetchone()
        if last and last[1]==sha:
            return last[0]
        seq=(last[0] if last else 0)+1
        db.execute('INSERT INTO special_confirmations VALUES(?,?,?,?,?,?,?,?)',(plan,version,store,seq,sha,wr.dumps(values),now(),actor))
        return seq


def observe(path,plan,entries):
    with connection(path,True) as db:
        body=read(db,plan)
        targets={(r['store'],r['sku']) for r in body['lines']}
        for row in entries:
            key=row['store'],row['sku']
            if key not in targets:
                raise ValueError('观测不属于本主单')
            snapshot=required(row.get('snapshot'),'门店快照原号')
            at=instant(row['at']).isoformat()
            for field in ('available','transit','promise','included'):
                if row.get(field) is not None:
                    number(row[field],field)
            sha=digest(row)
            old=db.execute('SELECT sha FROM special_observations WHERE plan=? AND store=? AND sku=? AND snapshot=?',(plan,*key,snapshot)).fetchone()
            if old and old[0]!=sha:
                raise wr.Conflict('同一快照原号已有不同观测，请登记新的快照版本')
            db.execute('INSERT OR IGNORE INTO special_observations VALUES(?,?,?,?,?,?,?)',(plan,*key,snapshot,at,sha,wr.dumps(row)))


def in_window(body,line,at):
    return max(instant(body['start']),instant(line.get('start',body['start'])))<=at<min(instant(body['end']),instant(line.get('end',body['end'])))
