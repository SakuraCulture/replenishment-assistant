"""Independent inventory workbook; existing funnel exports are untouched."""
from pathlib import Path
import hashlib
import os
import tempfile
from funnel_monitor_export import Sheet, MONEY, PERCENT, NUMBER
from xlsx_exports import stream_book
from runtime_support import checkpoint
from export_progress import RowProgress
import inventory_structure as service

ROUND_HEADERS=['门店编码','门店名称','业务日期','库存视角','签发时间','范围','大盘已计价货值','大盘完整货值','中心仓已计价货值','地采已计价货值','来源待核已计价货值','持有款数','未计价款数','数量待核款数','计价覆盖率','上轮日期','已计价部分增减','可比数量变化影响','可比价格变化影响']
DETAIL_FIELDS=['store','store_name','day','sku','name','unit','source','warehouse','status','group','stock','inbound','price','current_quantity','current_value','previous_quantity','previous_price','previous_value','quantity_effect','price_effect','change_kind','previous_source','previous_warehouse','previous_status','previous_group','observed_at','price_source','must_version','source_row']
DETAIL_HEADERS=['门店编码','门店名称','业务日期','商品编码','商品名称','库存单位','供货来源','归属仓','商品状态','必采属性','现货数量','在途数量','当时单价','本轮视角数量','本轮货值','上轮视角数量','上轮单价','上轮货值','数量变化影响','价格变化影响','变化类型','上轮来源','上轮仓','上轮状态','上轮必采属性','源表更新时间','价格依据','必采依据版本','来源行号','待核原因']


def round_values(r,view):
    c=r.get('changes') or {};m=r['total']
    return [r['store'],r['store_name'],r['day'],service.VIEWS[view],r.get('saved'),r['scope'],m['known_value'],m['value'],
        *[r['sources'][s]['known_value'] for s in service.SOURCES],m['held_skus'],m['unpriced'],m['invalid_quantity'],m['coverage'],r['previous_day'],r['delta'],c.get('quantity_effect'),c.get('price_effect')]


def export(path,report,target,include_details=False,progress=lambda _:None,cancel=None):
    target=Path(target).resolve()
    if target.suffix.lower()!='.xlsx' or target.exists():raise ValueError('请使用新的.xlsx文件保存分析快照')
    service.validate_basis(path,report,cancel=cancel);checkpoint(cancel)
    fd,tmp=tempfile.mkstemp(dir=target.parent,prefix='.inventory-',suffix='.xlsx');os.close(fd)
    names=[];view=report['query']['view']
    try:
        with stream_book() as book:
            sheet=Sheet(book,'货盘逐轮总览',[24,32,16,20,26,40]+[24]*13,cancel);sheet.row(ROUND_HEADERS,'header',height=55);names.append(sheet.ws.title)
            for r in report['rounds']:sheet.row(round_values(r,view),formats={**{i:MONEY for i in (6,7,8,9,10,16,17,18)},14:PERCENT})
            for name,key,keys in [('中心仓状态','statuses',service.STATUSES),('中心仓必采','groups',service.GROUPS)]:
                s=Sheet(book,name,[24,32,16,22,22,24,24,20,20,20,24,28,22],cancel);names.append(name)
                s.row(['门店编码','门店名称','业务日期','库存视角','分组','已计价货值','完整货值','持有款数','未计价款数','数量待核款数','已计价部分占比','较上轮已计价部分增减','供货仓'],'header',height=55)
                for r in report['rounds']:
                    for wh,data in [('全部中心仓',r),*r['warehouse_structures'].items()]:
                        for g in keys:
                            m=data[key][g];denom=data['sources']['中心仓']['known_value'];deltas=r['status_deltas' if key=='statuses' else 'group_deltas']
                            s.row([r['store'],r['store_name'],r['day'],service.VIEWS[view],g,m['known_value'],m['value'],m['held_skus'],m['unpriced'],m['invalid_quantity'],m['known_value']/denom if denom else None,deltas[g] if wh=='全部中心仓' else None,wh],formats={5:MONEY,6:MONEY,10:PERCENT,11:MONEY})
            if include_details:
                s=Sheet(book,'商品与变化明细',[24]*len(DETAIL_HEADERS),cancel);names.append(s.ws.title);s.row(DETAIL_HEADERS,'header',height=55)
                for i,r in enumerate(report['rounds']):
                    progress(RowProgress('写出商品与变化明细',i+1,len(report['rounds'])))
                    for d in service.detail(path,report,r['store'],r['day'],cancel=cancel):
                        s.row([*[d.get(k) for k in DETAIL_FIELDS],'；'.join(d['issues'])],formats={i:NUMBER for i in range(10,20)})
            s=Sheet(book,'口径与待核',[34,110],cancel);names.append(s.ws.title);s.row(['项目','说明'],'header')
            s.row(['指标版本',service.VERSION]);s.row(['分析快照',report['snapshot_id']]);s.row(['查询时间',report['as_of']]);s.row(['查询范围',f"{report['query']['from']} 至 {report['query']['to']} / {service.VIEWS[view]}"])
            s.row(['分仓小计','全部中心仓是山东仓和河北仓的合计，不与分仓行重复相加；分仓变化首期通过逐轮记录比较。'],height=55)
            labels=['统计范围','计价依据','货值含义','计价覆盖','独立维度','变化分解','分类迁移','跨店大盘','历史依据']
            for label,text in zip(labels,service.DEFINITIONS.values()):s.row([label,text],height=55)
            for r in report['rounds']:
                m=r['total'];s.row([r['store_name']+' / '+r['day'],f"{r['scope']}；正库存未计价{m['unpriced']}款，数量待核{m['invalid_quantity']}款，缺价{m['missing_price']}款（含零库存）；源表更新时间见按需明细，未提供不等于实时。"],height=55)
            progress('压缩保存货盘报表…');book.save(tmp)
        checkpoint(cancel);service.validate_basis(path,report,cancel=cancel);os.link(tmp,target)
    finally:Path(tmp).unlink(missing_ok=True)
    return {'path':str(target),'sha256':hashlib.sha256(target.read_bytes()).hexdigest(),'snapshot_id':report['snapshot_id'],'metric_version':service.VERSION,'sheets':names,'rounds':len(report['rounds'])}
