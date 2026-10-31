"""Versioned reusable stocking lists, exact Excel headers/codes and raw attachments."""
import copy
import hashlib
import sqlite3
import uuid
from contextlib import closing
from datetime import date
from pathlib import Path
import input_adapter as adapter
import new_product_focus
import work_repository as wr
from preissue import decimal, scalar, KINDS
from runtime_support import checkpoint


def inspect(paths, progress=lambda _:None, cancel=None):
    out=[]
    for i,path in enumerate(dict.fromkeys(str(Path(p).resolve()) for p in paths),1):
        checkpoint(cancel);progress(f'识别铺货清单 {i}：{Path(path).name}')
        out.append(new_product_focus.inspect(path,progress,cancel))
    return out


def revision(value):
    return hashlib.sha256(wr.dumps({k:v for k,v in value.items() if k not in ('revision','version','saved')}).encode()).hexdigest()


def validate(value):
    if value.get('schema')!=1 or value.get('kind') not in set(KINDS)-{'manual'}:raise ValueError('清单格式或类型无效')
    if not value.get('name','').strip() or len(value['name'])>100:raise ValueError('清单名称须为1～100字')
    if value.get('revision')!=revision(value):raise ValueError('铺货清单校验不一致')
    seen=set()
    for item in value['items']:
        sku,error=adapter.identifier(item.get('sku'))
        if error or sku!=item['sku'] or sku in seen:raise ValueError('清单商品编码缺失、重复或不可靠')
        seen.add(sku)
        if item.get('target') is not None and decimal(item['target'],'目标持有量')<=0:raise ValueError('目标持有量必须大于0')
    if not seen:raise ValueError('清单没有商品')
    defaults=value.get('defaults',{})
    if defaults.get('strategy','minimum') not in ('minimum','target'):raise ValueError('未知默认策略')
    if defaults.get('strategy')=='target':
        if defaults.get('target') is not None:
            if decimal(defaults['target'],'统一目标')<=0:raise ValueError('统一目标必须大于0')
        elif any(i.get('target') is None for i in value['items']):raise ValueError('部分商品没有目标，请填写统一目标或逐行填写目标')
    for k in ('valid_from','valid_to'):
        if value.get(k):date.fromisoformat(value[k])
    if value.get('valid_from') and value.get('valid_to') and value['valid_from']>value['valid_to']:raise ValueError('清单结束日期早于开始日期')
    return value


def retain(dbpath, value, progress=lambda _:None, cancel=None):
    import source_archive
    validate(value)
    for source in value['sources']:
        checkpoint(cancel)
        with closing(sqlite3.connect(dbpath,timeout=10)) as db:stored=source_archive.read(db,source['sha256'])
        if not stored:source_archive.capture(dbpath,source['path'],source['sha256'],progress,cancel)
    checkpoint(cancel)
    return value


def import_list(specs, name, kind, defaults=None, progress=lambda _:None, cancel=None):
    """Caller explicitly chooses worksheet and unique headers for each file."""
    items={};sources=[]
    for index,spec in enumerate(specs,1):
        checkpoint(cancel);path=Path(spec['path']);before=spec['sha256']
        if adapter.digest(path)!=before:raise ValueError('清单文件在预览后发生变化，请重新读取')
        mapping=spec['mapping'];fields=[v for v in mapping.values() if v]
        if not mapping.get('sku') or len(fields)!=len(set(fields)):raise ValueError('商品编码必填，各字段不能映射同一列')
        progress(f'读取清单 {index}/{len(specs)}：{path.name}')
        records,meta=adapter.read_table(path,spec['sheet'],[mapping['sku']])
        if any(meta['headers'].count(v)!=1 for v in fields):raise ValueError('所选表头缺失或重复，不能按列位置猜测')
        # An uncached formula must not masquerade as an intentionally empty target.
        import openpyxl
        formulas=openpyxl.load_workbook(path,read_only=True,data_only=False,keep_links=False)
        try:
            ws=formulas[spec['sheet']];ws.reset_dimensions();indices={name:meta['headers'].index(name) for name in fields};by_row={r['_row']:r for r in records}
            for rowno,cells in enumerate(ws.iter_rows(),1):
                checkpoint(cancel)
                if rowno not in by_row:continue
                for field,col in indices.items():
                    if col<len(cells) and cells[col].data_type=='f' and by_row[rowno].get(field) is None:
                        raise ValueError(f'{path.name} 第{rowno}行 {field} 公式无缓存值，请在Excel重新计算保存')
        finally:formulas.close()
        source={'path':str(path),'sha256':before,'sheet':spec['sheet'],'mapping':copy.deepcopy(mapping)};sources.append(source)
        for r in records:
            checkpoint(cancel);sku,error=adapter.identifier(r.get(mapping['sku']))
            if error:raise ValueError(f'{path.name} 第{r["_row"]}行：{error}')
            target=r.get(mapping.get('target'))
            if target in (None,''):target=None
            else:
                t=decimal(target,'目标持有量')
                if t<=0:raise ValueError('目标持有量必须大于0：'+sku)
                target=scalar(t)
            item={'sku':sku,'name':str(r.get(mapping.get('name')) or ''),'target':target,
                  'note':str(r.get(mapping.get('note')) or ''),'focused':str(r.get(mapping.get('focused')) or ''),
                  'source_rows':[{'source':index-1,'row':r['_row']}]}
            if sku in items:
                prev=items[sku]
                if any(prev[k]!=item[k] for k in ('target','focused')):raise ValueError('同商品目标/关注冲突，请在原清单明确选择后重新导入：'+sku)
                prev['source_rows'].extend(item['source_rows'])
            else:items[sku]=item
        if adapter.digest(path)!=before:raise ValueError('清单文件在读取期间发生变化，请重试')
    value={'schema':1,'id':uuid.uuid4().hex,'name':name.strip(),'kind':kind,'items':list(items.values()),'sources':sources,
           'defaults':copy.deepcopy(defaults or {'strategy':'minimum','target':None,'scopes':['zero']}),'valid_from':'','valid_to':''}
    value['revision']=revision(value)
    return validate(value)


class Repository:
    def __init__(self, dbpath):self.path=Path(dbpath)
    def all(self):
        with closing(sqlite3.connect(self.path,timeout=10)) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='stocking_lists'").fetchone():return []
            return [validate(wr.loads(row[0])) for row in db.execute('SELECT data FROM stocking_lists ORDER BY saved DESC,version DESC')]
    def save(self, value, expected=None, progress=lambda _:None, cancel=None):
        value=copy.deepcopy(validate(value));raw=[]
        for s in value['sources']:
            import source_archive
            checkpoint(cancel)
            with closing(sqlite3.connect(self.path,timeout=10)) as db:stored=source_archive.read(db,s['sha256'])
            data=stored[1] if stored else Path(s['path']).read_bytes()
            if hashlib.sha256(data).hexdigest()!=s['sha256']:raise ValueError('清单原文件在确认后变化，未保存')
            raw.append((s,data))
        checkpoint(cancel);progress('保存清单版本及原文件快照…')
        with closing(sqlite3.connect(self.path,timeout=10)) as db,db:
            db.execute('BEGIN IMMEDIATE');checkpoint(cancel)
            db.execute('CREATE TABLE IF NOT EXISTS stocking_lists(id TEXT,version INTEGER,revision TEXT,saved TEXT,data TEXT,PRIMARY KEY(id,version))')
            prev=db.execute('SELECT version,revision FROM stocking_lists WHERE id=? ORDER BY version DESC LIMIT 1',(value['id'],)).fetchone()
            if (prev[1] if prev else None)!=expected:raise wr.Conflict('清单版本已变化，请刷新后重新保存')
            value.update(version=prev[0]+1 if prev else 1,saved=wr.now())
            db.execute('CREATE TABLE IF NOT EXISTS source_files(sha TEXT PRIMARY KEY,name TEXT NOT NULL,saved TEXT NOT NULL,content BLOB NOT NULL)')
            for s,data in raw:
                checkpoint(cancel);db.execute('INSERT OR IGNORE INTO source_files VALUES(?,?,?,?)',(s['sha256'],Path(s['path']).name,value['saved'],data))
            db.execute('INSERT INTO stocking_lists VALUES(?,?,?,?,?)',(value['id'],value['version'],value['revision'],value['saved'],wr.dumps(value)))
            checkpoint(cancel)
        return value
