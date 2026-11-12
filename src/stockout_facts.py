"""Bounded native-file projection. No calculation, price filters, or business writes."""
from contextlib import contextmanager
import re
import sqlite3
from stockout_common import (digest, file_hash, number, date_of, encoded, now, resource_check,
                            WAREHOUSES, VIEWS, VERSION, TAG_VERSION)
from runtime_support import checkpoint
from warehouse_routing import select_warehouse
from virtual_categories import RETIRED
from stockout_tags import tokens as virtual_tokens, assessment as is_assessment

CATALOG_FIELDS=('商品编码','商品名称','商品状态','可用数','分类','供应商名称','虚拟分类')
STORE_FIELDS=('仓库/门店编码','商品编码','商品名称','月销','可用库存','在途库存','分类','补货状态')


def identifier(value):
    # Use the original precision contract, including rejection of >15-digit numbers.
    from input_adapter import identifier as original
    key, error = original(value)
    if error or key != key.strip():
        raise ValueError(error or '编码含前后空格')
    return key


@contextmanager
def table(path, required, cancel=None, *, used=()):
    import openpyxl
    from table_records import table_records
    from openpyxl.utils import get_column_letter
    book = openpyxl.load_workbook(path, read_only=True, data_only=False, keep_links=False)
    try:
        choices=[]
        for sheet in book:
            sheet.reset_dimensions()
            for index, row in enumerate(sheet.iter_rows(values_only=True),1):
                checkpoint(cancel)
                if index>50:
                    break
                heads=[str(v).strip() if v is not None else '' for v in row]
                if set(required)<=set(heads):
                    needed=set(required)|set(used)
                    # Same boundary as replenishment: duplicate unused native
                    # columns (e.g. the three 毛利 columns) are not ambiguous inputs.
                    # Validate optional fields too whenever analysis consumes them.
                    try:table_records(heads,(),sheet.title,[h for h in dict.fromkeys(heads) if h in needed])
                    except ValueError:
                        ambiguous=[h for h in dict.fromkeys(heads) if h in needed and heads.count(h)>1]
                        detail='、'.join(h+'（'+ '、'.join(get_column_letter(i+1) for i,v in enumerate(heads) if v==h)+'列）' for h in ambiguous)
                        raise ValueError(f'分析使用的表头重复：{detail}；工作表“{sheet.title}”第{index}行。无法确定应使用哪列，请核对导出格式；原文件未改动。') from None
                    choices.append((sheet,index,heads))
                    break
        if len(choices)!=1:
            raise ValueError('原件需要且只能有一张含这些表头的数据表：'+'、'.join(required))
        sheet,start,heads=choices[0]
        positions=[(i,h) for i,h in enumerate(heads) if h in set(required)|set(used)]
        def rows():
            iterator=sheet.iter_rows(min_row=start+1,values_only=True)
            try:
                for line,values in enumerate(iterator,start+1):
                    checkpoint(cancel)
                    if not any(v is not None and v!='' for v in values):
                        continue
                    if any(isinstance(v,str) and len(v)>65536 for v in values):
                        raise ValueError(f'第{line}行字段异常过长，请核对原件')
                    yield line,{h:values[i] if i<len(values) else None for i,h in positions}
            finally:iterator.close()
        records=rows()
        try:yield heads,records
        finally:records.close()
    finally:
        book.close()


def load_catalog(repo, source, warehouse, *, expected=None, expected_rows=None, observed=None, cancel=None, progress=lambda _:None):
    if warehouse not in WAREHOUSES:
        raise ValueError('请明确选择山东仓或河北仓')
    sha=expected or file_hash(source)
    path=repo.retain(source,sha,cancel)
    with repo.connect() as db:
        prior=db.execute('SELECT rows FROM catalogs WHERE sha=? AND warehouse=?',(sha,warehouse)).fetchone()
    if prior:
        if expected_rows is not None and prior[0]!=expected_rows:
            raise ValueError('仓原件条数与平台回执不一致')
        return sha
    count=0
    with table(path,['商品编码'],cancel,used=CATALOG_FIELDS) as (heads,rows):
        # Store rows under immutable source hash; incomplete projections are never adopted.
        with repo.connect(write=True) as db:
            db.execute('DELETE FROM catalog_items WHERE sha=?',(sha,))
        batch=[];batch_bytes=0
        for line,row in rows:
            sku=identifier(row.get('商品编码'))
            tags=str(row.get('虚拟分类') or '') if '虚拟分类' in heads else None
            batch.append((sha,sku,str(row.get('商品名称') or ''),str(row.get('商品状态') or ''),number(row.get('可用数')),
                          str(row.get('分类') or ''),str(row.get('供应商名称') or ''),tags,line))
            count+=1
            batch_bytes+=len(encoded(batch[-1]).encode('utf-8'))
            if len(batch)>=1000 or batch_bytes>=4*1024*1024:
                _catalog_batch(repo,batch);batch=[];batch_bytes=0;progress(f'{warehouse}已读取{count}款资料')
        if batch:
            _catalog_batch(repo,batch)
    if count<1 or (expected_rows is not None and count!=expected_rows) or file_hash(path)!=sha:
        raise ValueError('仓原件为空、条数不符或读取期间变化')
    with repo.connect(write=True) as db:
        db.execute('INSERT INTO catalogs VALUES(?,?,?,?)',(sha,warehouse,count,observed or now()))
    return sha


def _catalog_batch(repo,batch):
    resource_check(repo.folder)
    try:
        with repo.connect(write=True) as db:
            db.executemany('INSERT INTO catalog_items VALUES(?,?,?,?,?,?,?,?,?)',batch)
    except sqlite3.IntegrityError as exc:
        raise ValueError('仓商品编码重复，不能取第一条或重复扩行') from exc


def classify(db,sku,route,catalogs):
    pending=('pending','归属待核',None,'供货路线或所需仓资料缺失')
    if not route or route.get('primary') not in WAREHOUSES:
        return pending
    primary,secondary=route['primary'],route.get('secondary')
    if secondary not in (None,*WAREHOUSES) or secondary==primary:
        return pending
    if any(w not in catalogs for w in (primary,secondary) if w):
        return pending
    selected={w:db.execute('SELECT * FROM catalog_items WHERE sha=? AND sku=?',(catalogs[w],sku)).fetchone() for w in (primary,secondary) if w}
    p=selected[primary]
    state=('无该商品' if p is None else '禁用' if p['status']=='禁用' else
           '可用数待核' if p['stock'] is None else '有货' if p['stock']>0 else '无货')
    match=lambda r:'商品编码精确匹配' if r is not None else '资料未匹配'
    target,_=select_warehouse(primary,secondary,state,match(p),match(selected.get(secondary)))
    row=selected[target]
    if row is None:
        if not any(r is not None for r in selected.values()):
            return 'local','地采',None,'本次所选供货仓无该商品，按本次地采处理'
        return pending
    return 'center',target,row,f'按已采用供货设置归属{target}；主仓{state}'


def exclusion(view, prefix='', *, exclude_stopped=False, exclude_disabled=False):
    if view not in VIEWS:
        raise ValueError('未知剔除视角')
    r=f'coalesce({prefix}retired,0)=1'
    a=f'coalesce({prefix}assessment,0)=1'
    terms=[a]
    if view in ('retired','both'):terms.append(r)
    if exclude_stopped:terms.append(f"{prefix}replen_status IN ('停采','禁采')")
    if exclude_disabled:terms.append(f"{prefix}product_status='禁用'")
    return '('+' OR '.join(terms)+')'


def load_store(repo,rid,store,source,*,observed,expected=None,expected_rows=None,cancel=None,progress=lambda _:None):
    run=repo.run(rid)
    if run['state'] in {'COMPLETE','PARTIAL','ENDED'}:
        raise ValueError('已发布任务不可重新装载，请创建修订')
    basis=repo.settings(run['basis'])
    names={s['code']:s['name'] for s in basis['stores']}
    if store not in names:
        raise ValueError('原件门店不在本次明确选择范围')
    day=date_of(observed)
    sha=expected or file_hash(source)
    path=repo.retain(source,sha,cancel)
    identity=[store,day,observed,sha,run['basis'],run['catalogs'],VERSION,TAG_VERSION]
    if run['external'].get('recalculate'):identity.append(rid)
    sid=digest(identity)
    with repo.connect() as db:
        prior=db.execute('SELECT rows,state FROM store_snapshots WHERE id=?',(sid,)).fetchone()
        for warehouse,source_sha in run['catalogs'].items():
            if not db.execute('SELECT 1 FROM catalogs WHERE sha=? AND warehouse=?',(source_sha,warehouse)).fetchone():
                raise ValueError('仓资料投影尚未完整校验')
    if prior and prior['state']=='READY':
        if expected_rows is not None and prior['rows']!=expected_rows:
            raise ValueError('门店条数与原回执不符')
        repo.store_result(rid,store,sid)
        return sid
    with repo.connect(write=True) as db:
        db.execute('INSERT OR IGNORE INTO store_snapshots(id,store,name,day,observed,source_sha,basis,catalog_set,state) VALUES(?,?,?,?,?,?,?,?,?)',
                   (sid,store,names[store],day,observed,sha,run['basis'],encoded(run['catalogs']),'STAGING'))
        db.execute('DELETE FROM fact_records WHERE snapshot=?',(sid,))
        db.execute('DELETE FROM summaries WHERE snapshot=?',(sid,))
        db.execute('DELETE FROM dimension_summaries WHERE snapshot=?',(sid,))
    profile=basis.get('profile')
    mandatory={i['sku'] for i in profile['items']} if profile else None
    count=0;batch=[];batch_bytes=0
    with table(path,['仓库/门店编码','商品编码','月销','可用库存'],cancel,used=STORE_FIELDS) as (heads,rows), repo.connect() as reader:
        for line,row in rows:
            if identifier(row.get('仓库/门店编码'))!=store:
                raise ValueError(f'第{line}行出现其他门店，不能混入本店快照')
            sku=identifier(row.get('商品编码'))
            month,stock,inbound=(number(row.get(k)) for k in ('月销','可用库存','在途库存'))
            # Store stockouts use available stock floored at zero. Keep stock_raw
            # below exactly as supplied, including negatives, for traceability.
            if stock is not None:stock=max(0,stock)
            valid_month=month is not None and month>=0
            eligible=int(month>0) if valid_month else None
            bad=int(not valid_month or eligible==1 and stock is None)
            zero=int(eligible==1 and stock==0)
            source_type,warehouse,product,reason=classify(reader,sku,basis.get('routes',{}).get(store),run['catalogs'])
            tags=product['tags'] if product is not None else None
            tokens=virtual_tokens(tags)
            retired=int(bool(tokens & RETIRED)) if tokens is not None else None
            assessment=is_assessment(tags)
            category=(product['category'] if product is not None else str(row.get('分类') or '') if source_type=='local' else '') or '类目未提供'
            supplier=(warehouse+' · '+product['supplier'] if product is not None and product['supplier'] else '供货商未提供') if source_type=='center' else '不适用（地采）' if source_type=='local' else '供货商待核'
            errors=[reason]
            if assessment:errors.append('虚拟分类淘便考核（不计入基数）')
            if not valid_month:errors.append('月销缺失、非法或负数，有销基数待核')
            if stock is None:errors.append('门店可用库存缺失或非法，不能当作0')
            if tokens is None:errors.append('虚拟分类依据未取得，剔除视角仍保留此商品')
            fact=(sid,sku,str(row.get('商品名称') or ''),str(row.get('月销') if row.get('月销') is not None else ''),
                  str(row.get('可用库存') if row.get('可用库存') is not None else ''),str(row.get('在途库存') if row.get('在途库存') is not None else ''),
                  month,stock,inbound,eligible,bad,zero,source_type,warehouse,int(sku in mandatory) if mandatory is not None else None,
                  category,supplier,retired,assessment,tags,'；'.join(errors),line,
                  (product['status'] or '商品状态未提供') if product is not None else '商品状态未提供',
                  str(row.get('补货状态') or '补货状态未提供'))
            batch.append(fact);count+=1;batch_bytes+=len(encoded(fact).encode('utf-8'))
            if len(batch)>=1000 or batch_bytes>=4*1024*1024:
                _facts_batch(repo,batch);batch=[];batch_bytes=0;progress(f'{names[store]}已处理{count}款')
        if batch:_facts_batch(repo,batch)
    checkpoint(cancel)
    if count<1 or expected_rows is not None and count!=expected_rows or file_hash(path)!=sha:
        raise ValueError('门店原件为空、条数与平台不符或读取期间变化，未发布')
    with repo.connect(write=True) as db:
        db.execute('''INSERT INTO dimension_summaries SELECT snapshot,dimension,replen_status,
          CASE WHEN inbound IS NULL OR inbound<0 THEN '在途待核' WHEN inbound>0 THEN '有在途' ELSE '无在途' END,
          count(*),coalesce(sum(eligible),0),sum(zero),sum(bad) FROM fact_records WHERE snapshot=?
          GROUP BY snapshot,dimension,replen_status,4''',(sid,))
        for view in VIEWS:
            ex=exclusion(view)
            db.execute(f'''INSERT INTO summaries SELECT ?,?,
              coalesce(sum(eligible=1 AND NOT ({ex})),0), coalesce(sum(zero=1 AND NOT ({ex})),0),
              coalesce(sum(bad=1 AND NOT ({ex})),0),
              coalesce(sum(eligible=1 AND (retired IS NULL OR assessment IS NULL) AND NOT ({ex})),0),
              coalesce(sum(zero=1 AND (retired IS NULL OR assessment IS NULL) AND NOT ({ex})),0),
              coalesce(sum(eligible=1 AND ({ex})),0), coalesce(sum(zero=1 AND ({ex})),0)
              FROM facts WHERE snapshot=?''',(sid,view,sid))
        db.execute("UPDATE store_snapshots SET state='READY',rows=? WHERE id=?",(count,sid))
    repo.store_result(rid,store,sid)
    return sid


def _facts_batch(repo,batch):
    resource_check(repo.folder)
    try:
        with repo.connect(write=True) as db:
            dimensions={};records=[]
            for fact in batch:
                values=(*fact[12:21],fact[22]);key=digest(values)
                if key not in dimensions:
                    db.execute('INSERT OR IGNORE INTO fact_dimensions(digest,source,warehouse,mandatory,category,supplier,retired,assessment,tags,error,product_status) VALUES('+','.join('?' for _ in range(11))+')',(key,*values))
                    dimensions[key]=db.execute('SELECT id FROM fact_dimensions WHERE digest=?',(key,)).fetchone()[0]
                records.append((*fact[:12],dimensions[key],fact[21],fact[23]))
            db.executemany('INSERT INTO fact_records VALUES('+','.join('?' for _ in range(15))+')',records)
    except sqlite3.IntegrityError as exc:
        raise ValueError('同店商品编码重复，未发布；请核对原件完整性') from exc
