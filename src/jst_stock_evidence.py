"""Read-only export of the exact stock snapshots used by a preview or batch.

Never substitute the current warehouse default or baseline for a saved bid.
The workbook is an audit view, not an import template or a new allocation.
"""
import json
import sqlite3
import uuid
from decimal import Decimal, InvalidOperation
from itertools import chain
from contextlib import closing
from pathlib import Path

import jst_orders as orders
import jst_batches as batches
import warehouse_catalog as catalogs
from runtime_support import checkpoint
from export_files import publish_new
from xlsx_exports import stream_book, write_table


SUMMARY = ['状态','仓库','原文件','本机导入时间','库存依据建立时间','商品款数',
           '商品资料版本','分配批次号','批次创建时间','库存依据ID','原件SHA256','原工作表',
           '平台导出时间','原件归档记录','导出范围','说明']
ALLOCATION = ['仓库','门店编码','门店名称','补货单号','商品编码','商品名称','需求量',
              '箱规','资料可用','分配前余额','实际分配量','分配后余额','缺口','原因',
              '库存依据ID','分配批次号','生成记录ID','分配顺序','分配记录状态']
ITEM_PREFIX = ['库存依据ID','分配批次号','仓库']
ITEM_FIRST = ['商品编码','商品名称','可用数','出库箱规','基本售价','商品状态','分类',
              '商品等级','补货模型分类','实际库存数','采购在途数','订单占有数','商品标签']
LABELS = {'_row':'原表行号','_first_fill_source_field':'首铺原字段','_first_fill_issue':'首铺字段提示'}
LIMIT = 1048575


def connect(path):
    return sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=10)


def has(db, table):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone())


def _base(db, bid):
    if not has(db,'jst_baselines'):raise ValueError('没有留存分配库存依据，不能用当前资料代替')
    value=orders.decode(db.execute('SELECT payload,digest FROM jst_baselines WHERE bid=?',(bid,)).fetchone())
    if not value or value.get('bid')!=bid:raise ValueError('原库存依据不存在或身份不符：'+str(bid))
    catalogs.validate(value['catalog'])
    if value.get('warehouse')!=value['catalog']['warehouse']:raise ValueError('原库存依据所属仓不一致')
    return value


def _generation(db, gid):
    if not has(db,'jst_generations'):raise ValueError('没有留存分配结果')
    value=orders.decode(db.execute('SELECT payload,digest FROM jst_generations WHERE gid=?',(gid,)).fetchone())
    if not value or value.get('gid')!=gid:raise ValueError('所选生成记录不存在或身份不符：'+str(gid))
    import deletion_barrier
    deletion_barrier.require(db,value['head']['oid'],value['head']['warehouse'])
    return value


def _batch_info(db, batch_id):
    if not batch_id:return {'batch_id':'历史无分配批次','created':''}
    if not batches.exists(db):raise ValueError('原分配批次记录不存在')
    value=orders.decode(db.execute('SELECT payload,digest FROM jst_batches WHERE batch_id=?',(batch_id,)).fetchone())
    if not value or value.get('batch_id')!=batch_id:raise ValueError('原分配批次不存在或身份不符')
    return value


def _check_generation(value, base):
    if value['bid']!=base['bid'] or value['head']['warehouse']!=base['catalog']['warehouse']:
        raise ValueError('生成记录与原库存依据不符，未导出')
    meta=value.get('catalog') or {}
    if any(meta.get(k)!=base['catalog'].get(k) for k in ('warehouse','revision','source_sha256')):
        raise ValueError('生成记录的商品资料版本与原库存依据不符，未导出')


def _archived(db, sha):
    if not sha:return '未留存'
    for table in ('jst_files','source_files'):
        if has(db,table) and db.execute('SELECT 1 FROM '+table+' WHERE sha=?',(sha,)).fetchone():
            return '有留存记录；完整原件请用“查看当时资料依据”下载核验'
    return '未留存完整原件；本表仍提供当时实际采用字段'


def _value(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,allow_nan=False) if isinstance(value,(list,dict)) else value


def _number(value):
    if value is None or isinstance(value,bool):return value
    try:
        number=Decimal(str(value))
        if not number.is_finite() or len(number.normalize().as_tuple().digits)>15:return value
        return int(number) if number==number.to_integral_value() else number
    except (ValueError,InvalidOperation):return value


def _write(db, entries, plans, target, progress, cancel):
    checkpoint(cancel)
    if not entries:raise ValueError('当前范围没有实际选用的仓资料，请先预览或选择已保存批次')
    for entry in entries:entry['archive_record']=_archived(db,entry['base']['catalog'].get('source_sha256'))
    statuses={}
    for plan in plans:
        gid=plan.get('gid')
        current=has(db,'jst_active') and db.execute('SELECT 1 FROM jst_active WHERE gid=?',(gid,)).fetchone()
        saved=has(db,'jst_generations') and db.execute('SELECT 1 FROM jst_generations WHERE gid=?',(gid,)).fetchone()
        statuses[gid]='当前生效（导出读取时）' if current else '历史记录（已替换或撤销）' if saved else '未确认分配预览'
    # All exact immutable snapshots have now been captured consistently. Release
    # the read transaction before Excel I/O so a large export cannot hold up a
    # business writer. The final source fence is read again after saving.
    db.rollback()
    target=Path(target).resolve()
    if target.suffix.lower()!='.xlsx':raise ValueError('请选择.xlsx导出文件')
    if target.exists():raise FileExistsError('导出位置已有文件，请换一个新文件名；原文件不会覆盖')
    target.parent.mkdir(parents=True,exist_ok=True)
    temporary=target.with_name('.'+target.stem+'.'+uuid.uuid4().hex+'.tmp.xlsx')
    try:
        with stream_book() as book:
            def table(name,headers,rows,count=None):
                return write_table(book,name,headers,rows,style='single',cancel=cancel,progress=progress,row_count=count)
            def summary():
                for entry in entries:
                    base,c,info=entry['base'],entry['base']['catalog'],entry['batch']
                    yield [entry['state'],c['warehouse'],c.get('source_path'),c.get('imported_at'),base.get('created'),len(c['items']),
                           c['revision'],info['batch_id'],info.get('created'),base['bid'],c.get('source_sha256'),c.get('sheet'),
                           '未留存',entry['archive_record'],entry['scope'],
                           '本机导入时间不等于平台库存时点；资料版本相同不能证明没有刷新。采用字段不是原Excel全部列。'+
                           ('本预览尚未确认，未建立库存占用。' if entry['state']=='未确认预览' else '按原库存依据导出，未读取当前默认资料、未重新分配。')]
            table('资料批次',SUMMARY,summary(),len(entries))
            names={}
            for entry in entries:
                checkpoint(cancel);base=entry['base'];c=base['catalog'];items=c['items']
                keys=list(dict.fromkeys(chain((k for k in ITEM_FIRST if any(k in row for row in items.values())),(k for row in items.values() for k in row))))
                if len(keys)+len(ITEM_PREFIX)>16384:raise ValueError('采用字段超过Excel列数限制，未导出不完整资料')
                name='采用字段_'+c['warehouse'];names[name]=names.get(name,0)+1
                rows=iter(items.values());remaining=len(items);part=0
                while remaining:
                    part+=1;count=min(LIMIT,remaining)
                    def values(count=count):
                        for _ in range(count):
                            checkpoint(cancel);row=next(rows)
                            yield [_value(row.get(k)) for k in keys]+[base['bid'],entry['batch']['batch_id'],c['warehouse']]
                    table(name+'_'+str(names[name])+(('_'+str(part)) if part>1 else ''),[LABELS.get(k,k) for k in keys]+ITEM_PREFIX,values(),count)
                    remaining-=count
            def allocation():
                for plan in plans:
                    head=plan['head']
                    for row in plan['rows']:
                        checkpoint(cancel)
                        yield [head['warehouse'],head['store'],head.get('name',head['store']),head['number'],row['sku'],row.get('name'),
                               *[_number(row.get(k)) for k in ('demand','step','stock','before','quantity','after','missing')],
                               row.get('reason'),plan['bid'],plan.get('batch_id') or '历史无分配批次',plan.get('gid') or '',plan.get('position'),statuses[plan.get('gid')]]
            count=sum(len(p['rows']) for p in plans)
            if count>LIMIT:raise ValueError('分配核对明细超过Excel单表上限，请按更小范围导出')
            table('分配核对',ALLOCATION,allocation(),count)
            checkpoint(cancel);progress('正在保存实际采用资料核对表…');book.save(temporary)
        checkpoint(cancel)
        # Reject stale in-memory previews after a source warehouse was deleted.
        import deletion_barrier
        for plan in plans:deletion_barrier.require(db,plan['head']['oid'],plan['head']['warehouse'])
        publish_new(temporary,target)
        progress('已导出实际采用仓资料：'+str(target))
        return str(target)
    finally:
        temporary.unlink(missing_ok=True)


def export_preview(path, plan, target, progress=lambda _:None, cancel=None):
    """Export only the exact chosen preview; no registration or reservation."""
    checkpoint(cancel);value=plan['payload']
    if orders.digest(value)!=plan['digest']:raise ValueError('分配预览已变化，请重新预览后导出')
    plans=value['plans'];spec=value.get('new_batch');entries=[];seen=set()
    with closing(connect(path)) as db:
        db.execute('BEGIN')
        for p in plans:
            checkpoint(cancel);key=(p['bid'],p['head']['warehouse'])
            if key in seen:continue
            seen.add(key)
            if spec:
                base=spec['stocks'].get(p['head']['warehouse'])
                if not base:raise ValueError('预览缺少选用库存，不能替换为当前默认资料')
                catalogs.validate(base['catalog']);info=spec;state='未确认预览'
            else:
                base=_base(db,p['bid']);info=_batch_info(db,p.get('batch_id'));state='已冻结；本次为未确认分配预览'
            _check_generation(p,base)
            entries.append({'base':base,'batch':info,'state':state,'scope':'本次预览涉及仓资料；未确认，不作为已生成订单'})
        return _write(db,entries,plans,target,progress,cancel)


def export_batch(path, batch_id, target, progress=lambda _:None, cancel=None):
    """Export a saved batch's full frozen stock, including pending members."""
    checkpoint(cancel)
    with closing(connect(path)) as db:
        db.execute('BEGIN');spec=batches.context(db,batch_id,load_stock=False);entries=[];plans=[]
        for warehouse,(bid,_) in spec['states'].items():
            base=_base(db,bid)
            if base['catalog']['warehouse']!=warehouse:raise ValueError('批次仓与库存依据不一致')
            entries.append({'base':base,'batch':spec,'state':'已冻结','scope':'完整分配批次；包括尚未生成订单的选用仓资料'})
        if has(db,'jst_generation_batches'):
            for gid, in db.execute('SELECT b.gid FROM jst_generation_batches b JOIN jst_active a ON a.gid=b.gid WHERE b.batch_id=? ORDER BY b.position',(batch_id,)):
                p=_generation(db,gid);base=next((e['base'] for e in entries if e['base']['bid']==p['bid']),None)
                if base is None:raise ValueError('生成记录没有对应批次库存依据')
                _check_generation(p,base);plans.append(p)
        return _write(db,entries,plans,target,progress,cancel)


def export_generations(path, gids, target, progress=lambda _:None, cancel=None):
    """Export each saved generation's own bid, even if defaults changed later."""
    gids=list(gids)
    if not gids or len(gids)!=len(set(gids)):raise ValueError('请选择明确且不重复的已保存生成单')
    entries=[];plans=[];seen=set()
    with closing(connect(path)) as db:
        db.execute('BEGIN')
        for gid in gids:
            checkpoint(cancel);p=_generation(db,gid);plans.append(p)
            linked=batches.generation_batch(db,gid) if batches.exists(db) else None
            bid=p.get('batch_id') or linked
            if p.get('batch_id') and linked and linked!=p['batch_id']:raise ValueError('生成记录的分配批次关联不一致')
            key=(p['bid'],p['head']['warehouse'])
            if key in seen:continue
            seen.add(key);base=_base(db,p['bid']);_check_generation(p,base)
            entries.append({'base':base,'batch':_batch_info(db,bid),'state':'已冻结','scope':'所选生成单实际使用的资料；不同库存依据分表保留'})
        return _write(db,entries,plans,target,progress,cancel)
