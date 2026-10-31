"""Immutable merchant replies, bound to an exact signed revision, never model writes."""
import hashlib,io,math,sqlite3
from pathlib import Path
from contextlib import closing
import openpyxl
import work_repository as wr
import input_adapter as adapter
import normal_exports
import warehouse_orders as wo
import result_semantics as sem
import source_archive
from runtime_support import checkpoint

def connect(path):return sqlite3.connect(path,timeout=10)

def current(db,oid,revision):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='merchant_feedback'").fetchone():return None
    row=db.execute('SELECT version,saved,payload FROM merchant_feedback WHERE oid=? AND revision=? ORDER BY version DESC LIMIT 1',(oid,revision)).fetchone()
    return {'version':row[0],'saved':row[1],'payload':wr.loads(row[2])} if row else None

def scan(paths,progress=lambda _:None,cancel=None):
    files=[];records=[];orders={}
    for path in dict.fromkeys(str(Path(p).resolve()) for p in paths):
        checkpoint(cancel);progress('读取回传文件：'+Path(path).name);content=Path(path).read_bytes();sha=hashlib.sha256(content).hexdigest();files.append({'path':path,'sha':sha,'name':Path(path).name})
        wb=openpyxl.load_workbook(io.BytesIO(content),read_only=True,data_only=False,keep_links=False)
        props={p.name:p.value for p in wb.custom_doc_props};found=False
        try:
            for ws in wb:
                checkpoint(cancel);ws.reset_dimensions();iterator=ws.values;headers=list(next(iterator,()));qfield='门店确认数量' if '门店确认数量' in headers else '补货数量'
                required=['补货单号','商品编码',qfield]
                if not all(k in headers for k in required):continue
                found=True
                if any(headers.count(k)!=1 for k in required):raise ValueError(path+'：回传必需表头重复')
                for n,values in enumerate(iterator,2):
                    checkpoint(cancel)
                    if not any(v is not None and v!='' for v in values):continue
                    d=dict(zip(headers,values));number=d.get('补货单号');sku,error=adapter.identifier(d.get('商品编码'))
                    if not isinstance(number,str) or not number.strip() or error:raise ValueError(f'{Path(path).name} / {ws.title} 第{n}行：单号或商品编码无效')
                    rev=None
                    if props.get('修订'):
                        if props.get('补货单号')!=number:raise ValueError('回传单号与原导出修订信息不一致')
                        text=str(props['修订'])
                        if not text.startswith('V') or not text[1:].isdigit():raise ValueError('回传修订标识无效')
                        rev=int(text[1:])
                    value=d.get(qfield)
                    if value in (None,''):qty=None
                    else:
                        qty=adapter.numeric(value)
                        if isinstance(qty,bool) or not isinstance(qty,(int,float)) or not math.isfinite(qty) or qty<0:raise ValueError(f'{Path(path).name} / {ws.title} 第{n}行：确认数量须为非负数字或空白')
                    orders.setdefault(number,[])
                    if rev not in orders[number]:orders[number].append(rev)
                    records.append({'number':number,'revision':rev,'sku':sku,'quantity':qty,'note':d.get('备注'),'source':{'sha':sha,'file':Path(path).name,'sheet':ws.title,'row':n}})
        finally:wb.close()
        if not found:raise ValueError(Path(path).name+'：缺少补货单号、商品编码、门店确认数量（或补货数量）')
        if adapter.digest(path)!=sha:raise ValueError('读取期间回传文件变化')
    if not records:raise ValueError('回传文件没有商品数据')
    if any(len(set(r for r in revs if r is not None))>1 for revs in orders.values()):raise ValueError('同批文件包含同一单号的不同修订，请分开导入核对')
    return {'files':files,'records':records,'orders':orders}

def choices(path,numbers,progress=lambda _:None,cancel=None):
    result={}
    with closing(connect(path)) as db:
        for number in numbers:
            checkpoint(cancel)
            try:identity,warehouse=wo.resolve(db,number)
            except ValueError:result[number]=[];continue
            result[number]=[{'day':d,'store':s,'oid':o,'revision':v,'current':c,'saved':t,'warehouse':warehouse} for d,s,o,v,c,t in db.execute('SELECT o.day,o.store,o.oid,r.revision,o.current,r.saved FROM normal_orders o JOIN normal_revisions r ON o.oid=r.oid WHERE o.number=? ORDER BY r.revision DESC',(identity[2],))]
    return result

def prepare(path,scanned,bindings,progress=lambda _:None,cancel=None):
    from history_read import repository
    repo=repository(path);groups={};files=scanned['files']
    identities={}
    with closing(connect(path)) as db:
        for number in dict.fromkeys(item['number'] for item in scanned['records']):
            checkpoint(cancel);identities[number]=wo.resolve(db,number)
    for item in scanned['records']:
        revision=item['revision'] if item['revision'] is not None else bindings.get(item['number'])
        if not isinstance(revision,int) or revision<=0:raise ValueError('请选择明确的签发修订：'+item['number'])
        identity,warehouse=identities[item['number']]
        key=(identity[2],revision);groups.setdefault(key,[]).append({**item,'warehouse':warehouse})
    by_number={}
    for number,revision in groups:
        if number in by_number and by_number[number]!=revision:raise ValueError('同一单号的不同修订请分开导入核对')
        by_number[number]=revision
    plans=[]
    for (number,revision),items in groups.items():
        checkpoint(cancel);progress('核对回传差异：'+number+f' V{revision:03d}')
        with closing(connect(path)) as db:
            row=db.execute('SELECT day,store FROM normal_orders WHERE number=?',(number,)).fetchone()
        if not row:raise ValueError('本机未找到补货单号：'+number+'；不得按名称或相近日期猜测')
        order=repo.get(*row,revision,view='export');r=order['payload']['result'];eligible={d['E']:d for rows in normal_exports.groups(r,order['payload'].get('new_focus')).values() for d in rows}
        with closing(connect(path)) as db:
            old=current(db,order['oid'],revision)
            from archive_orders import require_active
            for wh in dict.fromkeys(eligible[i['sku']].get('AU') for i in items if i['sku'] in eligible):require_active(db,order['oid'],wh)
        values=dict(old['payload']['values']) if old else {};incoming={};warnings=[];duplicates=0
        for item in items:
            sku=item['sku'];qty=item['quantity'];checkpoint(cancel)
            if sku not in eligible:raise ValueError(number+'：商品不在该修订的中心仓确认单中：'+sku)
            if item['warehouse'] and eligible[sku].get('AU')!=item['warehouse']:raise ValueError(item['number']+'：商品不属于该签发修订的'+item['warehouse']+'：'+sku)
            if sku in incoming:
                if wr.dumps(incoming[sku]['quantity'])!=wr.dumps(qty):raise ValueError(number+'：同商品回传数量冲突：'+sku)
                incoming[sku]['sources'].append(item['source']);duplicates+=1;continue
            incoming[sku]={'quantity':qty,'note':item['note'],'sources':[item['source']],'warehouse':eligible[sku].get('AU'),'number':item['number']}
        differences=[]
        for sku,item in incoming.items():
            qty=item['quantity'];d=eligible[sku];before=values.get(sku)
            if qty is None:continue # blank never erases a previously returned number
            if before is None or wr.dumps(before)!=wr.dumps(qty):differences.append([sku,d.get('D'),sem.order_quantity(d),before,qty,qty-sem.order_quantity(d)])
            values[sku]=qty;box=d.get('BD')
            if sem.numeric(box) and box>1 and abs(qty/box-round(qty/box))>1e-9:warnings.append(sku+'：回传数非箱规倍数，按实记录，不自动规整')
        required=[s for s,d in eligible.items() if sem.order_quantity(d)>0];pending=[s for s in required if s not in values]
        unpriced=[s for s in values if not sem.numeric(eligible[s].get('V'))]
        amount=round(sum(qty*eligible[s]['V'] for s,qty in values.items() if s not in unpriced),2)
        if amount>r['summary']['amount']:warnings.append('回传按签发基本售价估算金额高于签发金额；不会重跑预算或修改数量')
        plans.append({'head':{k:v for k,v in order.items() if k!='payload'},'expected':old['version'] if old else None,'changed':bool(differences),'differences':differences,'warnings':warnings,'duplicates':duplicates,'payload':{'warehouses':wo.feedback_status(eligible,values),'values':values,'incoming':incoming,'files':[f for f in files if f['sha'] in {i['source']['sha'] for i in items}],'pending':pending,'status':'暂无待确认项' if not required else '部分确认' if pending else '已确认','estimated_amount':amount,'unpriced':unpriced}})
    return {'plans':plans,'files':files}

def commit(path,plan,progress=lambda _:None,cancel=None):
    # Entire batch atomic. UI disables cancellation only for this short commit call.
    checkpoint(cancel);contents=[]
    for f in plan['files']:
        data=Path(f['path']).read_bytes()
        if hashlib.sha256(data).hexdigest()!=f['sha']:raise ValueError('回传文件在核对后变化，必须重新预览')
        contents.append((f,data))
    encoded=[wr.dumps(p['payload']) for p in plan['plans']];checkpoint(cancel)
    import storage_chunks
    storage_chunks.backup_before_write(Path(path),progress)
    receipts=[]
    with closing(connect(path)) as db,db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('CREATE TABLE IF NOT EXISTS merchant_feedback(oid TEXT NOT NULL,revision INTEGER NOT NULL,version INTEGER NOT NULL,saved TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(oid,revision,version))')
        db.execute('CREATE TABLE IF NOT EXISTS source_files(sha TEXT PRIMARY KEY,name TEXT NOT NULL,saved TEXT NOT NULL,content BLOB NOT NULL)')
        for p in plan['plans']:
            h=p['head']
            from archive_orders import require_active
            for wh in dict.fromkeys(item.get('warehouse') for item in p['payload']['incoming'].values()):require_active(db,h['oid'],wh)
            now=db.execute('SELECT current FROM normal_orders WHERE oid=?',(h['oid'],)).fetchone();signed=db.execute('SELECT digest FROM normal_revisions WHERE oid=? AND revision=?',(h['oid'],h['revision'])).fetchone();old=current(db,h['oid'],h['revision'])
            if not now or now[0]!=h['current_revision'] or not signed or signed[0]!=h['digest']:raise wr.Conflict('签发记录在预览后变化，请重新核对回传')
            if (old['version'] if old else None)!=p['expected']:raise wr.Conflict('回传记录已被另一窗口更新，请重新核对')
        checkpoint(cancel);stamp=wr.now()
        for f,data in contents:db.execute('INSERT OR IGNORE INTO source_files VALUES(?,?,?,?)',(f['sha'],f['name'],stamp,data))
        for p,raw in zip(plan['plans'],encoded):
            h=p['head'];v=p['expected'] or 0
            if p['changed']:
                v+=1;db.execute('INSERT INTO merchant_feedback VALUES(?,?,?,?,?)',(h['oid'],h['revision'],v,stamp,raw))
                db.execute('INSERT INTO normal_events(at,oid,revision,action,detail) VALUES(?,?,?,?,?)',(stamp,h['oid'],h['revision'],'门店确认回传',wr.dumps({'number':h['number'],'store':h['store'],'feedback_version':v,'changed':len(p['differences']),'status':p['payload']['status'],'warehouses':p['payload']['warehouses'],'files':[f['name'] for f in p['payload']['files']]})))
            receipts.append({'number':h['number'],'revision':h['revision'],'feedback_version':v,'changed':p['changed'],'status':p['payload']['status'],'warehouses':p['payload']['warehouses']})
    return receipts

def view(path,head,progress=lambda _:None,cancel=None):
    from history_read import repository
    order=repository(path).get(head['day'],head['store'],head['revision'],view='export')
    if order['digest']!=head['digest']:raise ValueError('签发来源已变化')
    with closing(connect(path)) as db:
        record=current(db,head['oid'],head['revision']);history=[]
        if record:history=[(v,t,wr.loads(p)) for v,t,p in db.execute('SELECT version,saved,payload FROM merchant_feedback WHERE oid=? AND revision=? ORDER BY version DESC',(head['oid'],head['revision']))]
    values=record['payload']['values'] if record else {};rows=[]
    for sheet,items in normal_exports.groups(order['payload']['result'],order['payload'].get('new_focus')).items():
        for d in items:
            if head.get('warehouse') and d.get('AU')!=head['warehouse']:continue
            q=values.get(d['E']);issued=sem.order_quantity(d);rows.append([d['E'],d.get('D'),sheet,issued,q,None if q is None else q-issued,'未确认' if q is None else '已确认',d.get('AU')])
    return {'rows':rows,'history':history,'record':record}

def export_source(path,head,version,sha,target,progress=lambda _:None,cancel=None):
    progress('校验回传原文件并保存副本…')
    with closing(connect(path)) as db:
        row=db.execute('SELECT payload FROM merchant_feedback WHERE oid=? AND revision=? AND version=?',(head['oid'],head['revision'],version)).fetchone()
        if not row or sha not in {f['sha'] for f in wr.loads(row[0])['files']}:raise ValueError('此文件不属于所选回传版本')
        return source_archive.export(db,sha,target,cancel)
