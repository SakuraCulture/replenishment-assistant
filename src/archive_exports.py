"""Three-sheet warehouse archive, derived solely from an immutable signed revision."""
import io,os,re,tempfile
from pathlib import Path
from contextlib import closing
import openpyxl
from openpyxl.packaging.custom import StringProperty
import archive_orders as ao
import history_forms as forms
import normal_exports as ne
import result_semantics as sem
import source_archive
from runtime_support import checkpoint
from xlsx_exports import stream_book,write_table

LETTER_LABELS={'A': '仓库/门店名称', 'B': '仓库/门店编码', 'C': '供应商编码', 'D': '商品名称', 'E': '商品编码', 'F': '商品条码', 'G': '单位类型', 'H': '日销（原表14天日均输入）', 'I': '周销（最近7天累计）', 'J': '月销（最近30天累计）', 'K': '可用库存', 'L': '在途库存', 'M': '在途库存天数', 'N': '缺货天数（天）', 'O': '箱规', 'P': '周转天数', 'Q': '库存状态', 'R': '库存健康度', 'S': '中心仓状态', 'T': '商品标签', 'U': '分类', 'V': '基本售价', 'W': '近7天参考日均', 'X': '14天重组参考日均', 'Y': '30天重组参考日均', 'Z': '8至14天参考日均', 'AA': '15至30天参考日均', 'AB': 'α', 'AC': 'w1', 'AD': 'w2', 'AE': 'w3', 'AF': 'Z（原参数；新安全库存未使用）', 'AG': '备货深度', 'AH': '在途时长', 'AI': '周期性盘点临界天数', 'AJ': '周期性盘点受限价格', 'AK': '阶段离散度（参考）', 'AL': '安全库存', 'AM': '状态校正系数', 'AN': '校正综合日均', 'AO': '保护期预测基值', 'AP': '趋势后需求', 'AQ': '净需求（未整箱）', 'AR': '约束后候选量', 'AS': '补货数量（最终）', 'AT': '黑名单状态', 'AU': '所属仓', 'AV': '商品等级', 'AW': '校正状态', 'AX': '近7天有货天数', 'AY': '8至14天有货天数', 'AZ': '15至30天有货天数', 'BA': '计算用缺货天数', 'BB': '计算类型及异常标记', 'BC': '异常窗口参考日均', 'BD': '最小出库单位', 'BE': '分位查找起点', 'BF': '参考库存分位数', 'BG': '分位有效性', 'BH': '上限计算日均', 'BI': '原补货资格', 'BJ': '需求取整量', 'BK': '额度允许量', 'BL': '销售业务状态', 'BM': '补货结果说明', 'BN': '额度支持日均', 'BO': '极端缺货最低恢复量', 'BP': '资金约束前补货量', 'BQ': '本次最低恢复量', 'BR': '资金约束候选量', 'BS': '跨阶段支持销量', 'BT': '严格阶段库存金额上限', 'BU': '资金允许整箱数量', 'BV': '本次补货金额（元）', 'BW': '重点关注', 'BX': '待定额外数量'}

SHEETS=['完整计算建议表','制单时商品资料原表','模型建议签发表']


def flatten(value,prefix=()):
    if isinstance(value,dict):
        if not value:yield prefix,'空记录'
        for k,v in value.items():yield from flatten(v,prefix+(str(k),))
    elif isinstance(value,(list,tuple)):
        if not value:yield prefix,'空清单'
        for i,v in enumerate(value):yield from flatten(v,prefix+(str(i+1),))
    else:yield prefix,value


def label(path):
    return ' / '.join(LETTER_LABELS.get(k,forms.LABELS.get(k,k))+(' ['+k+']' if k in forms.LABELS or k in LETTER_LABELS else '') for k in path)


def calculation(order,wh):
    r=order['payload']['result'];rows=[d for d in r['rows'] if ao.warehouse(d)==wh]
    common={'签发单号':ao.number(order['number'],wh),'签发修订':order['revision'],'业务日期':order['day'],
        '全店共享预算（非分仓预算）':r.get('budget'), '当时实际设置':r.get('effective_settings'),
        '签发参数方案':order['payload'].get('preset'),'当时模型配置':r.get('config'),'仓周期':r.get('warehouses'),'一箱例外开关':r.get('enabled')}
    fixed=list(flatten(common,()));keys=list(dict.fromkeys(k for d in rows for k,v in flatten(d,())))
    from openpyxl.utils import column_index_from_string
    letters=sorted([k for k in keys if len(k)==1 and k[0] in LETTER_LABELS],key=lambda k:column_index_from_string(k[0]))
    keys=letters+[k for k in keys if k not in letters]
    headers=[label(k) for k,v in fixed]+[label(k) for k in keys]+['最终签发执行数','最终签发金额','核心决策理由']
    def values():
        for d in rows:
            flat=dict(flatten(d,()))
            yield [v for k,v in fixed]+[flat.get(k) for k in keys]+[sem.order_quantity(d),sem.order_amount(d),ne.decision(d)]
    return headers,values(),len(rows)


def checked_rows(rows):
    for row in rows:
        if any(isinstance(v,str) and len(v)>32767 for v in row):raise ValueError('字段超过Excel单元格上限，未截断导出')
        yield row


def original(repo,order,wh,cancel):
    parts=forms.work_parts(repo,order,[('catalogs',),('loaded','intake','products_snapshot')])
    catalog=parts.get(('catalogs',),{}).get(wh)
    if catalog:
        with closing(repo.work.connect()) as db:stored=source_archive.read(db,catalog.get('source_sha256'))
        if stored:
            book=openpyxl.load_workbook(io.BytesIO(stored[1]),read_only=True,data_only=False,keep_links=False)
            try:
                ws=book[catalog['sheet']];ws.reset_dimensions();rows=[]
                for values in ws.values:checkpoint(cancel);rows.append(list(values))
                return (rows[0],rows[1:]) if rows else (['原表为空'],[])
            finally:book.close()
        return ['留存说明','仓','签发时资料版本'],[['历史原文件未留存；本表不以当前资料或选取字段冒充原表',wh,catalog.get('revision')]]
    return ['留存说明','仓'],[['历史未留存可独立识别的本仓原表；不读取当前商品资料补填',wh]]


def filename(head,name):
    name=re.sub(r'[<>:"/\\|?*\x00-\x1f]','_',name).rstrip(' .')[:90]
    return head['day']+name+f'（归档·{head["warehouse"]}·V{head["revision"]:03d}）.xlsx'


def export(path,head,target,progress=lambda _:None,cancel=None):
    from history_read import repository
    repo=repository(path);wh=head['warehouse'];checkpoint(cancel)
    with closing(repo.work.connect()) as db:ao.require_active(db,head['oid'],wh)
    progress('读取并校验所选修订…');order=repo.get(head['day'],head['store'],head['revision'],progress=progress)
    if order is None or order['digest']!=head['digest']:raise ValueError('归档快照已变化，请重新查询')
    target=Path(target)
    if target.exists():raise FileExistsError('文件已存在，请换新文件名')
    if wh not in ao.scopes(order['payload']['result']['rows']):raise ValueError('此修订没有该仓数据')
    headers,rows,count=calculation(order,wh)
    progress('读取制单时商品资料原表…');raw_headers,raw_rows=original(repo,order,wh,cancel)
    grouped=ne.groups(order['payload']['result'],order['payload'].get('new_focus'))
    signed=[]
    for group,items in grouped.items():
        for d in items:
            if ao.warehouse(d)!=wh:continue
            status=d.get('AT');status='未提供' if status=='未提供（不作为数量拦截）' else status
            signed.append([group]+[d.get(k) for k in 'A B C D E F G H I J K L M N'.split()]+[d.get('BD'),d.get('V'),sem.order_quantity(d) if sem.orderable(d) else 0,status,d.get('AU'),d.get('AV'),ao.number(order['number'],wh),ne.decision(d)])
    target.parent.mkdir(parents=True,exist_ok=True);fd,tmp=tempfile.mkstemp(prefix='.archive-',suffix='.xlsx',dir=target.parent);os.close(fd)
    try:
        with stream_book() as book:
            for name,value in [('补货单号',ao.number(order['number'],wh)),('修订',f'V{order["revision"]:03d}'),('供货仓',wh),('来源指纹',order['digest'])]:book.custom_doc_props.append(StringProperty(name=name,value=value))
            for title,hs,rs,n in [(SHEETS[0],headers,rows,count),(SHEETS[1],raw_headers,raw_rows,len(raw_rows)),(SHEETS[2],['推荐类型']+ne.HEADERS+ne.AUDIT_HEADERS,signed,len(signed))]:
                if len(hs)>16384:raise ValueError('字段数超过Excel上限，未截断导出')
                write_table(book,title,hs,checked_rows(rs),cancel=cancel,progress=progress,row_count=n)
            progress('三张表写入完成，正在压缩保存（完成后响应取消）…');book.save(tmp);checkpoint(cancel)
            with closing(repo.work.connect()) as db:ao.require_active(db,head['oid'],wh)
            os.link(tmp,target)
    finally:Path(tmp).unlink(missing_ok=True)
    return str(target)
