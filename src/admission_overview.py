"""Read-only current eligibility overview; does not rewrite admission evidence."""
import copy,json,os,sqlite3,tempfile
from contextlib import closing
from pathlib import Path
import admission
from runtime_support import checkpoint

QUAL_HEADERS=['门店编码','门店名称','当前有效状态','登记状态','核验人','核验日期','到期日','拒收中款数','备注']
REF_HEADERS=['门店编码','门店名称','商品编码','商品名称','拒收原因','到期日','当前状态']


def qualification_state(value,day):
    if value.get('expires') and value['expires']<day:return '已到期'
    return '有效' if value.get('state')=='已核验' else value.get('state','待核验')


def refusal_state(value,day):
    return '已解除' if not value.get('active',True) else '已到期' if value.get('expires') and value['expires']<day else '拒收中'


def record_name(record):
    if record.get('name'):return str(record['name'])
    candidates=[record.get('qualification',{}),*record.get('refusals',{}).values()]
    for item in candidates:
        name=item.get('source',{}).get('original',{}).get('门店名称')
        if name:return str(name)
    return ''


def load(path,known=None,progress=lambda _:None,cancel=None,*,day=None):
    """Single SQLite read transaction, plus optional local directory, no database creation."""
    path=Path(path);day=day or admission.today();names=dict(known or {});records={};warnings=[]
    checkpoint(cancel);progress('正在读取门店准入现状…')
    if path.exists():
        with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=3)) as db:
            db.execute('BEGIN');tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'stores' in tables:
                for code,raw in db.execute('SELECT code,data FROM stores ORDER BY rowid'):
                    checkpoint(cancel);r=json.loads(raw)
                    if not names.get(code):names[code]=r.get('name','')
            if 'store_admission' in tables:
                for code,raw in db.execute('SELECT store,payload FROM store_admission ORDER BY rowid'):
                    checkpoint(cancel);records[code]=json.loads(raw)
    from aoxiang_directory import Directory
    try:
        directory=Directory(path.parent).load()
        for item in (directory or {}).get('items',[]):
            if not names.get(item['code']):names[item['code']]=item['name']
    except (OSError,ValueError,KeyError,TypeError) as exc:warnings.append('门店目录未能读取，以下仍展示作业、参数和准入记录中的门店：'+str(exc))
    for code,r in records.items():
        if not names.get(code):names[code]=record_name(r)
    qualifications=[];refusals=[]
    for n,(code,name) in enumerate(names.items(),1):
        checkpoint(cancel);r=records.get(code,{})
        try:admission.validate(r)
        except Exception as exc:raise ValueError(f'门店 {code} 准入资料异常，未将其当作有效资料：{exc}') from exc
        q=r.get('qualification',{});active=0
        for sku,item in r.get('refusals',{}).items():
            state=refusal_state(item,day);active+=state=='拒收中'
            refusals.append([code,name,sku,item.get('name',''),item['reason'],item.get('expires') or '手动解除',state])
        qualifications.append([code,name,qualification_state(q,day),q.get('state','未登记'),q.get('reviewer',''),q.get('verified_on') or '',q.get('expires') or '',active,q.get('note','')])
        if n%100==0:progress(f'已整理 {n}/{len(names)} 家门店')
    return {'day':day,'names':names,'qualification':qualifications,'refusals':refusals,'warnings':warnings,'records':records}


def prepare_release(data,keys,progress=lambda _:None,cancel=None):
    """Freeze an exact store/SKU selection against the displayed read snapshot."""
    selected=list(dict.fromkeys(tuple(k) for k in keys));old={};new={};rows=[]
    if not selected:raise ValueError('请先勾选要解除的门店商品')
    for n,(store,sku) in enumerate(selected,1):
        checkpoint(cancel)
        record=data['records'].get(store,{})
        item=record.get('refusals',{}).get(sku)
        if not item or not item.get('active',True):raise ValueError('记录已不存在或已解除，请刷新后重选：'+store+' / '+sku)
        if store not in old:old[store]=copy.deepcopy(record);new[store]=copy.deepcopy(record)
        new[store]['refusals'][sku]['active']=False
        rows.append([store,data['names'].get(store,''),sku,item.get('name',''),item['reason'],item.get('expires') or '手动解除',refusal_state(item,data['day']),'已解除'])
        if n%200==0:progress(f'正在核对解除范围 {n}/{len(selected)} 条…')
    return {'expected':old,'values':new,'rows':rows,'count':len(selected),'stores':list(old)}


def commit_release(path,prepared,progress=lambda _:None,cancel=None):
    checkpoint(cancel);progress(f'正在提交 {prepared["count"]} 条解除记录（{len(prepared["stores"])} 家门店），请等待提交结束…')
    admission.save(path,prepared['values'],prepared['expected'])
    progress(f'已解除 {prepared["count"]} 条；正在刷新列表…')
    return {'count':prepared['count'],'stores':prepared['stores']}


def select_rows(rows,kind,state='全部',text='',focus=None):
    terms=text.lower().split();status_col=2 if kind=='qualification' else 6
    return [r for r in rows if (state=='全部' or r[status_col]==state)
            and (focus is None or (r[0],None if kind=='qualification' else r[2]) in focus)
            and all(t in ' '.join(str(v) for v in r).lower() for t in terms)]


def export(path,kind,rows,scope,progress=lambda _:None,cancel=None):
    """Export precisely the displayed snapshot, not a fresh query with different filters."""
    from openpyxl import Workbook
    from openpyxl.styles import Font,PatternFill
    target=Path(path);title='二类医疗资质' if kind=='qualification' else '门店拒收清单'
    headers=QUAL_HEADERS if kind=='qualification' else REF_HEADERS
    wb=Workbook();ws=wb.active;ws.title=title;ws.append(headers);ws.freeze_panes='C2'
    for index,row in enumerate(rows,1):
        checkpoint(cancel);ws.append(row)
        for cell in ws[ws.max_row]:
            if isinstance(cell.value,str):cell.data_type='s';cell.number_format='@'
        if index%200==0:progress(f'正在导出 {index}/{len(rows)} 条记录…')
    for cell in ws[1]:
        cell.font=Font(bold=True,color='FFFFFF');cell.fill=PatternFill('solid',fgColor='126C63');ws.column_dimensions[cell.column_letter].width=22
    ws.auto_filter.ref=ws.dimensions
    note=wb.create_sheet('查询范围')
    for k,v in scope.items():
        note.append([k,str(v)])
        for cell in note[note.max_row]:cell.data_type='s'
    note.append(['说明','当前状态按查询日判断；到期当天仍有效。该文件用于核对现状，不是导入模板。'])
    note.column_dimensions['A'].width=22;note.column_dimensions['B'].width=90
    fd,tmp=tempfile.mkstemp(dir=target.parent,suffix='.xlsx');os.close(fd)
    try:progress(f'保存 {len(rows)} 条查询结果…');wb.save(tmp);checkpoint(cancel);os.replace(tmp,target)
    finally:wb.close();Path(tmp).unlink(missing_ok=True)
    return str(target)
