"""Externally labelled warehouse snapshots and demand ownership, not stock reservation."""
from __future__ import annotations
import copy,hashlib,json,os,re,sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from contextlib import closing,contextmanager,ExitStack
import openpyxl
from openpyxl.cell import WriteOnlyCell
import engine
from priority_list import atomic_json
from first_fill import FIELD as FIRST_FILL_FIELD,FIELDS as FIRST_FILL_FIELDS
import warehouse_newness as newness

from app_defaults import WAREHOUSES
from data_contracts import InputBatch
_UNSET=object()
_CURRENT_FIELD='_current_warehouse_catalog'
_CURRENT_PREFIX=b'{\n  "_current_warehouse_catalog": 1,'
REQUIRED=['商品编码','分类','基本售价','出库箱规','商品状态','可用数']
FIELDS=REQUIRED+['商品名称','商品等级','补货模型分类','实际库存数','采购在途数','订单占有数']
ARCHIVE_ALIASES={'商品名称':'商品名','分类':'产品分类','商品状态':'启用',
                 '实际库存数':'主仓实际库存','采购在途数':'采购在途'}
ARCHIVE_REQUIRED=[ARCHIVE_ALIASES.get(key,key) for key in REQUIRED]+[
    '商品名','商品创建日期','商品标签','主仓实际库存']
ARCHIVE_EXTRA=('主仓实际库存','标准装箱数量',newness.CREATED,newness.FIRST_INBOUND)
ORPHAN_REASON='备用、可用数为0，商品名、基本售价、建档时间均缺失'
ORPHAN_STOCK_REASON='备用商品有仓库存，商品名、基本售价、建档时间均缺失'

def revision(items):return hashlib.sha256(json.dumps(items,ensure_ascii=False,sort_keys=True,allow_nan=False).encode()).hexdigest()[:16]

def validate(data):
    if not isinstance(data,dict) or data.get('schema')!=1 or data.get('warehouse') not in WAREHOUSES or not isinstance(data.get('items'),dict) or not data['items']:raise ValueError('仓资料快照格式错误')
    for sku,row in data['items'].items():
        if not isinstance(row,dict) or engine.adapter.identifier(row.get('商品编码'))!=(sku,None):raise ValueError('仓资料存在无效商品编码')
        if not all(k in row for k in REQUIRED):raise ValueError('仓资料缺少必需字段')
    if revision(data['items'])!=data.get('revision'):raise ValueError('仓资料版本与内容不一致，请重新导入')
    if data.get('kind')=='jst_full_archive':
        pending=data.get('quarantined')
        if (not isinstance(pending,list) or data.get('source_rows')!=len(pending)+len(data['items'])
                or data.get('quarantine_count')!=len(pending) or type(data.get('first_inbound_column')) is not bool):
            raise ValueError('全商品档案有效与待核记录条数不一致')
        seen=set(data['items'])
        for row in pending:
            sku=row.get('商品编码')
            if not isinstance(sku,str) or not sku.strip() or sku in seen or not row.get('reason'):
                raise ValueError('全商品档案待核记录存在无效编码、重复或缺失原因')
            seen.add(sku)
    return data

def archive_evidence(path,warehouse,source_rows,evidence):
    if evidence is not None:
        q=evidence.get('query',{})
        default_scope=(evidence.get('reportScope')=='account_default'
                       and 'reportWarehouse' not in evidence and 'reportWarehouseId' not in evidence)
        selected_scope='reportScope' not in evidence and bool(evidence.get('reportWarehouse'))
        if (evidence.get('kind')!='jst_full_archive' or evidence.get('warehouse')!=warehouse
                or evidence.get('expectedRows')!=source_rows or not (default_scope or selected_scope)
                or not isinstance(evidence.get('company'),str) or not evidence['company'].strip()
                or q.get('sales')!='unlimited' or q.get('enabled')!='all'
                or q.get('excludeZero') is not False or q.get('excludeCancelledBeforeShipment') is not True
                or not re.fullmatch(r'[1-9]\d*',str(evidence.get('taskId','')))
                or not evidence.get('queriedAt') or not evidence.get('exportedAt')):
            raise ValueError('全商品档案的网页仓库、完整查询条件或原生导出回执未通过核验')
        saved={key:evidence[key] for key in ('warehouse','company','query','queriedAt','exportedAt','taskId','expectedRows')}
        for key in ('reportScope','reportWarehouse','reportWarehouseId'):
            if key in evidence:saved[key]=evidence[key]
        return saved
    observation={'warehouse':warehouse,'query':None,'expectedRows':source_rows,'scope':'manual'}
    name=re.fullmatch(r'商品主题分析_全商品档案_(\d{14})_([1-9]\d*)_1(?: \(\d+\))?',path.stem)
    if name:
        try:
            at=datetime.strptime(name[1],'%Y%m%d%H%M%S').isoformat()+'+08:00'
        except ValueError:
            return observation
        observation.update(exportedAt=at,taskId=name[2],identifiers_from='原文件名')
    return observation


def import_catalog(path,warehouse,progress=lambda s:None,cancel=None,*,evidence=None):
    if warehouse not in WAREHOUSES:raise ValueError('请明确选择河北仓或山东仓')
    path=Path(path).resolve();before=engine.adapter.digest(path)
    progress('读取'+warehouse+'商品资料…')
    sheet,heads,rows,info=engine.read_identified(path,'products',REQUIRED,alternate=ARCHIVE_REQUIRED)
    archive=info['alternate']
    if evidence is not None and (not archive or newness.FIRST_INBOUND not in heads):
        raise ValueError('本次自动同步需要包含首次入库时间的全商品档案')
    used=set(FIELDS)|{'商品标签','标准装箱数量'}
    if archive:
        used.update(ARCHIVE_ALIASES.values());used.update(ARCHIVE_EXTRA)
        if any(k in heads and v in heads for k,v in ARCHIVE_ALIASES.items()):
            raise ValueError('全商品档案存在重复语义的字段，请使用平台原件')
    if used&set(info['duplicate_unused_headers']):raise ValueError('商品资料存在重复业务表头')
    items={};pending=[];seen={}
    for raw in rows:
        engine.checkpoint(cancel);sku,error=engine.adapter.identifier(raw.get('商品编码'))
        if archive and not isinstance(raw.get('商品编码'),str):
            error='全商品档案须保留平台原文本编码'
        if error:raise ValueError(f"第{raw['_row']}行商品编码待核：{error}；未更新旧资料")
        if sku in seen:raise ValueError(f"本仓商品编码重复：{sku}，第{seen[sku]}和{raw['_row']}行；请处理后导入")
        seen[sku]=raw['_row']
        row={k:raw.get(ARCHIVE_ALIASES.get(k,k) if archive else k) for k in FIELDS}
        row.update(商品编码=sku,_row=raw['_row'])
        if archive:
            if row['商品状态'] not in ('启用','备用','禁用'):
                raise ValueError(f"第{raw['_row']}行商品状态无法识别：{sku}")
            if row['分类']=='分类-未设置':row['分类']=None
            for name in ARCHIVE_EXTRA:
                if name in heads:row[name]=newness.json_cell(raw.get(name))
            available=engine.adapter.numeric(row['可用数'])
            if (row['商品状态']=='备用' and available is not None and available>=0 and newness.blank(raw.get('商品标签'))
                    and all(newness.blank(row.get(key)) for key in ('商品名称','基本售价',newness.CREATED))):
                pending.append({**row,'reason':ORPHAN_REASON if available==0 else ORPHAN_STOCK_REASON})
                continue
        if '标准装箱数量' in heads:row['标准装箱数量']=raw.get('标准装箱数量')
        items[sku]=row
        if '商品标签' in heads:row['商品标签']=raw.get('商品标签')
        first_fill_fields=[f for f in FIRST_FILL_FIELDS if f in heads]
        if len(first_fill_fields)>1 or any(f in info['duplicate_unused_headers'] for f in first_fill_fields):
            row['_first_fill_issue']='“'+'／'.join(first_fill_fields)+'”表头重复或存在多个同义列，未自动择一'
        elif first_fill_fields:
            source=first_fill_fields[0];row[FIRST_FILL_FIELD]=raw.get(source);row['_first_fill_source_field']=source
    if before!=engine.adapter.digest(path):raise ValueError('读取期间文件发生变化，请重新导入')
    data={'schema':1,'warehouse':warehouse,'source_path':str(path),'source_sha256':before,'sheet':sheet,'imported_at':datetime.now().isoformat(timespec='seconds'),'items':items,'revision':revision(items)}
    if archive:
        data.update(kind='jst_full_archive',first_inbound_column=newness.FIRST_INBOUND in heads,
                    source_rows=len(rows),quarantined=pending,quarantine_count=len(pending),
                    source_observation=archive_evidence(path,warehouse,len(rows),evidence))
    return validate(data)

def metadata(catalog):return {k:v for k,v in catalog.items() if k not in ('items','quarantined')}|{'count':len(catalog['items'])}

def execution_warning(catalog):
    missing=sum('商品标签' not in row for row in catalog['items'].values())
    return f'{missing:,}款缺少“商品标签”字段，不能作新的正数执行安排；请补齐标签列。列存在且内容为空表示无标签，缺列不能视为空标签。' if missing else ''


def stock_publication_warning(catalog, cancel=None):
    """Keep the previous current version if an automatic capture has no usable stock."""
    from jst_allocation import number, status_issue
    orderable = 0
    for item in catalog['items'].values():
        engine.checkpoint(cancel)
        if '可用数' not in item:
            return '商品资料缺少“可用数”列，未更新当前仓资料'
        if status_issue(item):
            continue
        orderable += 1
        try:
            number(item['可用数'], '可用库存')
        except ValueError:
            continue
        return ''
    if orderable:
        return '启用及备用商品的可用数全部缺失或无效，未更新当前仓资料；请核对原件'
    return ''


class CatalogRevisionCheck:
    """One validated read per commit; short OS locks + file identities per order.

    All supported catalog writers replace files under the same warehouse locks.
    Identity checks also reject normal out-of-band replacements/in-place edits.
    No SKU tree or persisted cache is retained after this commit call.
    """
    def __init__(self,folder):
        self.repository=Repository(folder);self.tokens=None;self.revisions=None
    def identities(self):
        values={}
        for wh in WAREHOUSES:
            p=self.repository.folder/'warehouse_catalogs'/(wh+'.json')
            try:s=p.stat();values[wh]=(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
            except FileNotFoundError:values[wh]=None
        return values
    def verify(self):
        if self.identities()!=self.tokens:
            from work_repository import Conflict
            raise Conflict('仓资料标签依据已更新，请重新预览分配')
    @contextmanager
    def guard(self,required,cancel=None):
        if not required:yield None;return
        from catalog_write_lock import acquire
        with ExitStack() as locks:
            for wh in WAREHOUSES:locks.enter_context(acquire(self.repository.folder,wh,cancel))
            if self.tokens is None:
                tokens=self.identities()
                revisions={w:c['revision'] for w,c in self.repository.catalogs().items()}
                if self.identities()!=tokens:
                    from work_repository import Conflict
                    raise Conflict('读取期间仓资料已变化，请重新预览分配')
                self.tokens=tokens;self.revisions=revisions
            self.verify()
            yield self.revisions

def _history_identity(stat):
    # Windows volume/file IDs may exceed QVariant's signed 64-bit integer.
    # Keep exact opaque tokens as strings when the directory enters Qt userData.
    return [str(v) for v in (stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns)]


def _history_file(path):
    # Bind digest and decoded data to the same opened file. Keep no raw bytes
    # while validating the decoded tree, and reject an in-place/replaced file.
    before=_history_identity(path.stat())
    with path.open('rb') as source:
        opened=_history_identity(os.fstat(source.fileno()));raw=source.read()
        digest=hashlib.sha256(raw).hexdigest();text=raw.decode('utf-8');del raw
        data=validate(json.loads(text));del text
        # Windows stat and fstat expose different legacy st_ctime meanings.
        # Compare each API to itself; cross-check file identity/size/mtime only.
        after=_history_identity(path.stat())
        if opened!=_history_identity(os.fstat(source.fileno())) or before!=after or opened[:4]!=after[:4]:
            raise ValueError('历史仓资料在读取期间变化，请重新打开恢复窗口')
    return data,before,digest


class Repository:
    def __init__(self,folder):self.folder=Path(folder)

    def _file(self,warehouse):
        if warehouse not in WAREHOUSES:raise ValueError('未知仓库')
        return self.folder/'warehouse_catalogs'/(warehouse+'.json')

    def _legacy_stock(self,warehouse,file_time,load):
        # Before this update, the last allocation refresh could have been
        # stored only in SQLite. Read that old head without changing its ledger.
        path=self.folder/'workbench.sqlite3'
        if not path.is_file():return None
        with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=.2)) as db:
            db.execute('BEGIN')
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='jst_warehouses'").fetchone():return None
            row=db.execute('SELECT b.bid,b.created,b.digest FROM jst_warehouses w JOIN jst_baselines b ON b.bid=w.bid WHERE w.warehouse=?',(warehouse,)).fetchone()
            if not row:return None
            bid,created,digest=row
            if file_time is not None:
                try:adopted=datetime.fromisoformat(created).timestamp()
                except (TypeError,ValueError,OverflowError,OSError):return None
                if adopted<=file_time:return None
            data=None
            if load:
                from jst_order_store import decode
                base=decode(db.execute('SELECT payload,digest FROM jst_baselines WHERE bid=?',(bid,)).fetchone())
                data=validate(base['catalog'])
                if base['warehouse']!=warehouse or data['warehouse']!=warehouse:raise ValueError('历史库存依据所属仓不一致')
            return data,'legacy:'+bid+':'+digest

    def _current(self,warehouse,*,load=True):
        path=self._file(warehouse)
        try:
            with path.open('rb') as stream:
                raw=stream.read();file_time=os.fstat(stream.fileno()).st_mtime
        except FileNotFoundError:raw=None;file_time=None
        # The marker is written first by atomic_json. Modern digest checks do
        # not deserialize the full product catalog merely to compare versions.
        file_digest=hashlib.sha256(raw).hexdigest() if raw is not None else None
        if raw is None or not raw[:64].replace(b'\r\n',b'\n').startswith(_CURRENT_PREFIX):
            legacy=self._legacy_stock(warehouse,file_time,load)
            if legacy is not None:return (*legacy,file_digest)
        if raw is None:return None,None,None
        data=None
        if load:
            data=validate(json.loads(raw));data.pop(_CURRENT_FIELD,None)
            if data['warehouse']!=warehouse:raise ValueError('仓资料标签与保存位置不一致')
        return data,file_digest,file_digest

    def snapshots(self,warehouses=WAREHOUSES):
        catalogs={};versions={}
        for warehouse in warehouses:
            data,versions[warehouse],_=self._current(warehouse)
            if data is not None:catalogs[warehouse]=data
        return catalogs,versions

    def catalogs(self):
        return self.snapshots()[0]

    def current_digest(self,warehouse):
        return self._current(warehouse,load=False)[1]

    def source_reference(self,warehouse):
        """The file and, before the first publication, the selected legacy head."""
        _,current,file_digest=self._current(warehouse,load=False)
        result={'sha256':file_digest}
        if current and current.startswith('legacy:'):
            _,bid,digest=current.split(':')
            result['legacy']=[bid,digest]
        return result

    @staticmethod
    def document(data):
        return {_CURRENT_FIELD:1,**{k:v for k,v in data.items() if k!=_CURRENT_FIELD}}

    def publication_digest(self,data):
        raw=json.dumps(self.document(data),ensure_ascii=False,indent=2).replace('\n',os.linesep).encode('utf-8')
        return hashlib.sha256(raw).hexdigest()

    def save(self,data,*,expected_digest=_UNSET,cancel=None):
        from catalog_write_lock import acquire
        validate(data)
        with acquire(self.folder,data['warehouse'],cancel):
            return self.save_locked(data,expected_digest=expected_digest,cancel=cancel)
    def save_locked(self,data,*,expected_digest=_UNSET,cancel=None):
        """Caller owns the per-warehouse lock; one shared persistence implementation."""
        validate(data)
        engine.checkpoint(cancel)
        if expected_digest is not _UNSET and self.current_digest(data['warehouse'])!=expected_digest:
            raise ValueError('采集期间仓资料已被更新，未覆盖其他窗口的新资料')
        # Retain immutable prior snapshots before changing the current pointer file.
        old,_,_=self._current(data['warehouse'])
        for item in [old,data]:
            if item:
                key=hashlib.sha256(json.dumps(item,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
                p=self.folder/'warehouse_catalogs'/'history'/data['warehouse']/(key+'.json')
                if not p.exists():atomic_json(p,item)
        engine.checkpoint(cancel)
        atomic_json(self._file(data['warehouse']),self.document(data))
    def history(self,warehouse):
        if warehouse not in WAREHOUSES:raise ValueError('未知仓库')
        folder=self.folder/'warehouse_catalogs'/'history'/warehouse
        return [validate(json.loads(p.read_text('utf-8'))) for p in sorted(folder.glob('*.json'),key=lambda p:p.stat().st_mtime_ns,reverse=True)]
    def history_entries(self,warehouse,cancel=None):
        """Validate each original version, retaining only the displayed directory."""
        if warehouse not in WAREHOUSES:raise ValueError('未知仓库')
        folder=self.folder/'warehouse_catalogs'/'history'/warehouse;entries=[]
        for path in sorted(folder.glob('*.json'),key=lambda p:p.stat().st_mtime_ns,reverse=True):
            engine.checkpoint(cancel)
            data,identity,digest=_history_file(path)
            entries.append({'warehouse':warehouse,'file':str(path.absolute()),'identity':identity,'sha256':digest,
                            'metadata':{'imported_at':data['imported_at'],'revision':data['revision'],'count':len(data['items'])}})
            # Do not keep the preceding full catalog while reading the next.
            del data
        return entries
    def read_history_entry(self,entry,cancel=None):
        """Load the exact selected version; never substitute a newer snapshot."""
        warehouse=entry['warehouse']
        if warehouse not in WAREHOUSES:raise ValueError('未知仓库')
        path=Path(entry['file']);folder=self.folder/'warehouse_catalogs'/'history'/warehouse
        if path.parent.resolve()!=folder.resolve() or path.suffix.lower()!='.json':raise ValueError('历史仓资料位置不符，请重新打开恢复窗口')
        engine.checkpoint(cancel)
        try:
            if _history_identity(path.stat())!=entry['identity']:raise ValueError('所选历史仓资料已变化，请重新打开恢复窗口')
            data,identity,digest=_history_file(path)
        except FileNotFoundError as exc:raise ValueError('所选历史仓资料已移除，请重新打开恢复窗口') from exc
        meta={'imported_at':data['imported_at'],'revision':data['revision'],'count':len(data['items'])}
        if identity!=entry['identity'] or digest!=entry['sha256'] or meta!=entry['metadata']:
            raise ValueError('所选历史仓资料已变化，请重新打开恢复窗口')
        engine.checkpoint(cancel)
        return data
    def routes(self):
        p=self.folder/'store_supply.json'
        if not p.exists():return {}
        data=json.loads(p.read_text(encoding='utf-8'))
        if not isinstance(data,dict):raise ValueError('门店主从仓设置格式错误')
        for route in data.values():validate_route(route,WAREHOUSES)
        return data
    def save_route(self,store,route):
        validate_route(route,WAREHOUSES);data=self.routes();data[store]=route;atomic_json(self.folder/'store_supply.json',data)

def validate_route(route,catalogs):
    if not isinstance(route,dict) or route.get('primary') not in WAREHOUSES:raise ValueError('请选择本店主仓')
    primary=route['primary'];secondary=route.get('secondary')
    if secondary not in (None,*WAREHOUSES) or secondary==primary:raise ValueError('从仓必须与主仓不同，单仓供货请选择“无从仓”')
    for wh in [primary,secondary]:
        if wh and wh not in catalogs:raise ValueError(wh+'资料尚未导入，请先维护该仓资料')
    return route

def write_catalog(catalog,path):
    fields=FIELDS+([FIRST_FILL_FIELD] if any(FIRST_FILL_FIELD in r for r in catalog['items'].values()) else [])
    book=openpyxl.Workbook(write_only=True);ws=book.create_sheet('商品资料');ws.append(fields+['仓归属'])
    for row in catalog['items'].values():
        cells=[]
        for value in [row.get(k) for k in fields]+[catalog['warehouse']]:
            cell=WriteOnlyCell(ws,value)
            if isinstance(value,str):cell.data_type='s'
            cells.append(cell)
        ws.append(cells)
    book.save(path);book.close()

def prepare_warehouses(sales_path,catalogs,tmp,progress,cancel):
    """Run the existing adapter separately per warehouse, preventing cross-warehouse duplicate keys."""
    def load(wh,catalog):
        path=Path(tmp)/(wh+'.xlsx');write_catalog(catalog,path)
        return engine.load_inputs(sales_path,path,progress,cancel)
    return _prepare_catalogs(catalogs,load)


def prepare_records(sales_table,catalogs,progress=lambda s:None,cancel=None):
    """No temporary Excel: same per-warehouse adapter and provenance assembly."""
    from table_records import bridge_table
    def load(wh,catalog):
        raw=({**row,'仓归属':wh} for row in catalog['items'].values())
        fields=FIELDS+([FIRST_FILL_FIELD] if any(FIRST_FILL_FIELD in r for r in catalog['items'].values()) else [])
        table=bridge_table(fields+['仓归属'],raw,'商品资料')
        return engine.load_records(sales_table,table,sales_ref='memory:batch.xlsx',product_ref='memory:'+wh+'.xlsx',progress=progress,cancel=cancel)
    return _prepare_catalogs(catalogs,load)


def _prepare_catalogs(catalogs,load):
    results={};base=None
    for wh,catalog in catalogs.items():
        validate(catalog);loaded=load(wh,catalog)
        for d,m in loaded['normalized']:
            row=catalog['items'].get(d['E'])
            m.update(product_file=catalog['source_path'],product_sheet=catalog['sheet'],product_revision=catalog['revision'])
            from first_fill import basis
            m['first_fill']=basis(catalog,wh,d['E'])
            if row:
                m.update(product_row=row['_row'],warehouse_available=engine.adapter.numeric(row.get('可用数')))
                inbound=newness.source(catalog,row)
                if inbound is not None:m['first_inbound_source']=inbound
                if any('商品标签' in c['items'].get(d['E'],{}) for c in catalogs.values()):
                    from admission import catalog_meta
                    m.update(catalog_meta(catalogs,wh,d['E']))
            if d['_source']=='资料匹配':d['AU']=wh
        results[wh]=loaded['normalized']
        if base is None:base=loaded
        else:base['config']['category_profiles'].update(loaded['config']['category_profiles'])
    if base is None:raise ValueError('请至少导入一份带仓库标签的商品资料')
    lists=list(results.values());preview=[]
    for i in range(len(lists[0])):
        pairs=[rs[i] for rs in lists]
        preview.append(copy.deepcopy(next((pair for pair in pairs if pair[0]['_source']=='资料匹配'),pairs[0])))
    base.update(normalized=preview,warehouse_rows=results,catalogs={wh:metadata(c) for wh,c in catalogs.items()},warehouses=list(catalogs))
    return base

def assessment(pair):
    d,m=pair
    if m['match']!='商品编码精确匹配':return '无该商品'
    if str(d.get('S') or '').strip()=='禁用':return '禁用'
    available=m.get('warehouse_available')
    if not engine.numeric(available):return '可用数待核'
    return '有货' if available>0 else '无货'

def route_store(loaded:InputBatch,store:str,route) -> InputBatch:
    validate_route(route,loaded['catalogs']);primary=route['primary'];secondary=route.get('secondary')
    variants={w:[x for x in rows if x[0]['B']==store] for w,rows in loaded['warehouse_rows'].items()};base=[p for p in loaded['normalized'] if p[0]['B']==store];rows=[]
    for i,original in enumerate(base):
        p=variants[primary][i];state=assessment(p)
        # Availability chooses between existing catalog entries. A missing child
        # entry must not discard the primary's price/carton/status/provenance.
        from warehouse_routing import select_warehouse
        target,retain_primary=select_warehouse(primary,secondary,state,p[1]['match'],
            variants[secondary][i][1]['match'] if secondary else None)
        # Product ownership is scoped to this store's selected supply route.
        # An imported but unselected warehouse cannot turn local goods into a
        # matching error or supply its price/carton to a single-warehouse job.
        d,m=copy.deepcopy(variants[target][i]);known=any(variants[w][i][1]['match']=='商品编码精确匹配' for w in (primary,secondary) if w)
        child=assessment(variants[secondary][i]) if secondary else '未配置'
        reason=f'主仓{state}，需求归属'+('从仓' if target!=primary else '主仓')
        if target!=primary:reason+=f'；从仓{child}'
        if retain_primary:reason=f'主仓{state}，从仓无该商品，保留主仓归属及资料'
        if state=='可用数待核':reason='主仓可用数待核，保留主仓归属；未将缺失库存当成0'
        if known and m['match']!='商品编码精确匹配':
            d['_source']='匹配待核';m['blocking']=[s for s in m['blocking'] if s!='待补商品资料'];m['blocking'].append('归属仓未匹配商品资料，价格与箱规待核')
        d['AU']='地采' if d['_source']=='地采商品' else target
        if not known:reason='已导入的仓资料均未匹配，标记地采' if d['_source']=='地采商品' else '商品编码或资料匹配待核'
        single_local=secondary is None and d['_source']=='地采商品'
        if single_local:reason=f'本次仅由{primary}供货，该仓无此商品，按本次地采处理'
        m['routing']={'primary':primary,'secondary':secondary,'assigned':d['AU'],'primary_state':state,'secondary_state':child,'primary_stock':p[1].get('warehouse_available'),'secondary_stock':variants[secondary][i][1].get('warehouse_available') if secondary else None,'reason':reason,'version':'0.5.2' if single_local else '0.5.1' if retain_primary else '0.5.0'}
        rows.append((d,m))
    return {**loaded,'normalized':rows,'supply_route':copy.deepcopy(route)}
