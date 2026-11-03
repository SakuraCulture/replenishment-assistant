"""Workbook association metadata, independent of editable merchandise rows.

Not authentication. Structural protection reduces accidental removal only.
Conflicting evidence is an error; missing/ambiguous evidence requires binding.
"""
import hashlib,json
SHEET='系统关联信息'
VERSION='replenishment-order/1'
HEADERS=['格式版本','文件关联标识','业务工作表','补货单号','门店编码','仓库','业务日期','内部签发引用','原签发摘要','签发保存时间','关联信息校验']
KEYS=['version','file_id','sheet','number','store','warehouse','day','revision','digest','created']

def checksum(row):return hashlib.sha256(json.dumps(row,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()).hexdigest()

def entry(order,warehouse,sheet):
    return {'sheet':sheet,'number':order['number'],'store':order['store'],'warehouse':warehouse or '',
            'day':order['day'],'revision':order['revision'],'digest':order['digest'],'created':order.get('saved')}

def write(book,entries):
    if SHEET in book.sheetnames:del book[SHEET]
    ws=book.create_sheet(SHEET);ws.append(HEADERS)
    identity=checksum([[e.get(k) for k in ('sheet','number','store','warehouse','day','revision','digest','created')] for e in entries])
    for e in entries:
        row=[VERSION,identity,e['sheet'],e['number'],e['store'],e['warehouse'] or None,e['day'],e['revision'],e['digest'],e.get('created')]
        ws.append(row+[checksum(row)])
        for c in ws[ws.max_row]:
            if isinstance(c.value,str):c.data_type='s'
    ws.sheet_state='veryHidden';book.security.lockStructure=True

def read(book):
    if SHEET not in book.sheetnames:return []
    ws=book[SHEET]
    if hasattr(ws,'reset_dimensions'):ws.reset_dimensions()
    it=iter(ws.values)
    if list(next(it,()))!=HEADERS:raise ValueError('系统关联信息表头异常，请使用原签发文件或明确核对来源')
    entries=[];seen=set();ids=set()
    for values in it:
        if not any(x is not None for x in values):continue
        row=list(values)
        if len(row)!=len(HEADERS) or row[0]!=VERSION or checksum(row[:-1])!=row[-1]:raise ValueError('系统关联信息校验失败，不能猜测订单')
        e=dict(zip(KEYS,row));e['warehouse']=e['warehouse'] or '';ids.add(e['file_id'])
        if e['sheet'] not in book.sheetnames or e['sheet']==SHEET:raise ValueError('关联信息所指业务工作表不存在')
        if not e['number'] or not e['store'] or e['warehouse'] not in ('','山东仓','河北仓') or type(e['revision']) is not int or e['revision']<1 or not e['digest']:raise ValueError('系统订单关联信息不完整')
        key=(e['sheet'],e['number'])
        if key in seen:raise ValueError('系统关联信息重复')
        seen.add(key);entries.append(e)
    if not entries or len(ids)!=1:raise ValueError('系统关联信息为空或混入其他文件标识')
    props={p.name:p.value for p in book.custom_doc_props}
    if props.get('补货单号') and any(e['number']!=props['补货单号'] for e in entries):raise ValueError('隐藏页与旧文件单号属性冲突')
    def rev(v):return int(str(v).lstrip('Vv'))
    if props.get('修订') and any(e['revision']!=rev(props['修订']) for e in entries):raise ValueError('隐藏页与旧文件修订属性冲突')
    if props.get('签发修订索引'):
        index=json.loads(props['签发修订索引'])
        if {e['number']:e['revision'] for e in entries}!={n:rev(r) for n,r in index.items()}:raise ValueError('隐藏页与合并文件索引冲突')
    references={}
    for e in entries:
        ref=tuple(e[k] for k in ('store','warehouse','day','revision','digest'))
        if e['number'] in references and references[e['number']]!=ref:raise ValueError('同单号的隐藏关联依据冲突')
        references[e['number']]=ref
    return entries

def resolve(entries,sheet,number='',store='',warehouse=''):
    pool=[e for e in entries if e['sheet']==sheet]
    if not pool:return None,[]
    choices=[e for e in pool if all(not value or value==e[key] or key=='warehouse' and not e[key] for key,value in [('number',number),('store',store),('warehouse',warehouse)])]
    if not choices:raise ValueError('行单号、门店或仓与隐藏关联信息冲突')
    return (choices[0],[]) if len(choices)==1 else (None,choices)
