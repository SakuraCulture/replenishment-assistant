"""Merchant template: central-warehouse rows; audit detail stays in the archive."""
import os,re,tempfile
from pathlib import Path
from openpyxl import Workbook
from openpyxl.styles import Font,Color
from openpyxl.comments import Comment
from openpyxl.packaging.custom import StringProperty
import result_semantics as sem
import new_products
import new_product_focus as focus
import export_progress as ep
from io_stages import stage
from contextlib import closing
from openpyxl.cell import WriteOnlyCell
from adaptive_resources import Budget

HEADERS=['仓库/门店名称','仓库/门店编码','供应商编码','商品名称','商品编码','商品条码','基础单位','日销','周销','月销','可用库存','在途库存','在途库存天数','缺货天数（天）','箱规','中心仓价格','补货数量','黑名单状态','所属仓','商品等级']
AUDIT_HEADERS=['补货单号','决策理由']
MERCHANT_AUDIT_HEADERS=['决策理由']
SHEETS=list(sem.SIGNED_GROUPS)
TEMPLATE_SHA256='1cc7a8e9f99fec764d17286c6b0a028a7ea79416add7178a64e91512f311f7c6'


def filename(order,warehouse=None):
    name=re.sub(r'[<>:"/\\|?*\x00-\x1f]','_',order['payload']['result']['store_name']).rstrip(' .')[:100] or order['store']
    if warehouse:
        from warehouse_orders import child_number
        child_number('',warehouse)
    return order['day']+name+'（精简版'+('·'+warehouse if warehouse else '')+'）.xlsx'


groups=sem.signed_groups


def decision(d):
    """Short explanation of existing decisions only; never changes quantity."""
    excluded=sem.exclusion_reason(d)
    if '二类医疗资质' in excluded:return '资质不符'
    if '商品标签资料不完整' in excluded:return '资料待核'
    if '门店拒收' in excluded:return '门店拒收'
    if '日配' in excluded:return '日配不补'
    if '禁采' in excluded:return '禁采不补'
    if excluded:return '禁用不补' if '禁用' in excluded else '停采不补'
    if sem.issue_text(d):return '资料待核'
    adjustment=d.get('_preissue',{})
    if adjustment.get('delta'):
        if adjustment['kind']=='manual':return '人工不补' if adjustment['quantity']==0 else '人工增量' if adjustment['delta']>0 else '人工减量'
        return {'new':'新品补足','plan':'计划补足','promotion':'活动补足'}[adjustment['kind']]
    if adjustment.get('kind')=='manual' and adjustment.get('quantity')==0:return '人工不补'
    if d.get('_budget_overrun'):return '预算补足'
    if d.get('_budget_gap',0)>0:return '预算限量' if sem.orderable(d) else '预算暂缓'
    if d.get('_mandatory_holding',{}).get('added_quantity',0)>0 and sem.orderable(d):return '必采最低持有'
    if new_products.is_new(d):
        if d.get('_new_requested',0)>0:return '必卖首铺' if d.get('_must_sell') else '新品首铺'
        return '新品未选'
    if d.get('_audit',{}).get('applied'):return '一箱恢复'
    if d.get('AW')=='极端缺货自动拉回':return '极端恢复已关闭' if d.get('_extreme_restore',{}).get('enabled') is False else '极端缺货恢复'
    from model import floor_reason
    reason=floor_reason(d)
    if reason:return ('必卖补货' if d.get('_must_sell') else '动销补货') if reason=='正常需求补货' else reason
    # Counterfactual quantity without the holding floor, from persisted model
    # intermediates. Do not re-run prediction or infer from AS == BQ alone.
    keys=('AR','BI','BP','BQ','BR','BU','AS')
    if all(sem.numeric(d.get(k)) for k in keys):
        ordinary=d['AR'] if d['BI']==1 else 0
        without_floor=max(0,min(ordinary,d['BU']))
        raised=d['AS']>without_floor and (d['BQ']>without_floor or d['BP']>ordinary)
        if raised:
            if d.get('J')==0 and sem.numeric(d.get('N')) and d['N']>0:return '零销恢复'
            return '最低持有' if d['BQ']>without_floor else '断货保底'
    elif sem.numeric(d.get('BQ')) and d['BQ']>0:return '补货建议'
    return '必卖补货' if d.get('_must_sell') else '动销补货'


class _ExportPressure(Exception):
    pass


def _write_workbook(order,grouped,path,cancel,progress,warehouse,resources,streamed):
    from engine import checkpoint
    from xlsx_exports import stream_book
    owner=stream_book() if streamed else closing(Workbook())
    rev=f"V{order['revision']:03d}";historical=order['revision']!=order['current_revision'];font=Font(name='宋体',sz=11,charset=134,scheme='minor',color=Color(theme=1))
    remaining=sum(len(rows)+1 for rows in grouped.values())*len(HEADERS+MERCHANT_AUDIT_HEADERS)
    with owner as wb:
        styles={}
        if not streamed:wb.remove(wb.active)
        wb.properties.title=order['number']+' '+rev;wb.properties.subject='内部签发建议，非实际采购订单';wb.properties.description='仅中心仓商品；Q列可供门店调整；完整签发建议与来源保存在应用历史归档。数量为库存单位，价格为基本售价。'
        for key,value in [('补货单号',order['number']),('修订',rev),('模板SHA256',TEMPLATE_SHA256),('版本状态','历史修订' if historical else '当前修订')]:wb.custom_doc_props.append(StringProperty(name=key,value=value))
        if warehouse:
            parent=order['parent_number']
            for key,value in [('门店正常单号',parent),('供货仓',warehouse)]:wb.custom_doc_props.append(StringProperty(name=key,value=value))
        for name,rows in grouped.items():
            ep.rows(progress,name,0,len(rows));ws=wb.create_sheet(name);ws.sheet_format.defaultColWidth=9;ws.sheet_format.defaultRowHeight=13.5
            ws.oddHeader.center.text=order['number']+' '+rev+(' 历史修订' if historical else '')
            ws.column_dimensions['P'].width=12;ws.column_dimensions['R'].width=14;ws.column_dimensions['U'].width=14
            ws.print_options.horizontalCentered=False
            def append(values,rn,comments=None):
                cells=[]
                for col,value in enumerate(values,1):
                    cell=WriteOnlyCell(ws,value) if streamed else ws.cell(rn,col,value)
                    number_format=('0.00' if col==16 else '@') if rn>1 and col in (16,18) else None
                    # Binding a date/time may already have set a number format.
                    # Preserve that base style, and never share IDs across books.
                    key=(tuple(cell._style) if cell._style is not None else None,number_format)
                    cached=styles.get(key)
                    if cached is None:
                        cell.font=font
                        if number_format is not None:cell.number_format=number_format
                        styles[key]=cell._style
                    else:cell._style=cached  # Complete arrays are read-only after caching.
                    if isinstance(value,str):cell.data_type='s'
                    if comments and col in comments:cell.comment=Comment(comments[col],'补货助手')
                    cells.append(cell)
                if streamed:ws.append(cells)
            append(HEADERS+MERCHANT_AUDIT_HEADERS,1,{
                1:order['number']+' '+rev+'；业务日期 '+order['day']+'；'+('历史修订' if historical else '当前修订'),
                17:'签发执行数量（库存单位）；未选择或预算未安排的新品为0。门店可修改本列，原签发建议保存在应用历史归档。箱规品请填写箱规倍数。',
                16:'取商品资料基本售价；缺价保持空白或待核，不补造0。'})
            remaining-=len(HEADERS+MERCHANT_AUDIT_HEADERS)
            for rn,d in enumerate(rows,2):
                checkpoint(cancel);qty=sem.order_quantity(d) if sem.orderable(d) else 0
                reason=sem.exclusion_reason(d);status=d.get('AT')
                if status=='未提供（不作为数量拦截）':status='未提供'
                visible=[d.get(k) for k in 'A B C D E F G H I J K L M N'.split()]+[d.get('BD'),d.get('V'),qty,status,d.get('AU'),d.get('AV')]
                append(visible+[decision(d)],rn,{17:sem.explanation(d)} if reason or qty==0 else None)
                remaining-=len(HEADERS+MERCHANT_AUDIT_HEADERS)
                if (rn-1)%500==0:
                    ep.rows(progress,name,rn-1,len(rows))
                    if not streamed and not resources.export_fits(remaining):raise _ExportPressure()
                    if streamed:resources.refresh()
            ep.rows(progress,name,len(rows),len(rows));progress(name+'：整理格式…')
        import order_identity
        identities=[order_identity.entry(order,warehouse,name) for name in grouped]
        order_identity.write(wb,identities)
        progress('数据行写入完成，正在压缩保存确认单；此阶段暂不能中断…')
        with stage('压缩保存 Excel（完成后响应取消）',progress):wb.save(path)
    return identities


def export(order,path,cancel=None,progress=lambda s:None,*,warehouse=None,resources=None):
    from engine import checkpoint
    path=Path(path);checkpoint(cancel);result=order['payload']['result'];profile=order['payload'].get('new_focus');focus.require_for(profile,order['day'],[result])
    if warehouse:
        from warehouse_orders import partitions,child_number
        grouped=partitions(result,profile).get(warehouse)
        if grouped is None:raise ValueError('该修订没有可导出的'+warehouse+'商品')
        order={**order,'parent_number':order['number'],'number':child_number(order['number'],warehouse)}
    else:grouped=groups(result,profile)
    resources=resources or Budget()
    streamed=not resources.export_fits(sum(len(rows)+1 for rows in grouped.values())*len(HEADERS+MERCHANT_AUDIT_HEADERS))
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,temp=tempfile.mkstemp(prefix='.normal-',suffix='.xlsx',dir=path.parent);os.close(fd)
    try:
        while True:
            progress('导出资源自适应：'+('逐行写入' if streamed else '内存写入')+'；'+resources.description())
            retry=False
            try:identities=_write_workbook(order,grouped,temp,cancel,progress,warehouse,resources,streamed)
            except (_ExportPressure,MemoryError) as exc:
                if streamed:raise
                if isinstance(exc,MemoryError):resources.emergency()
                retry=True
            if not retry:break
            # Only an unpublished, private workbook is rebuilt. Signed records,
            # output publication and other batch members are never replayed.
            import gc
            gc.collect();checkpoint(cancel);streamed=True
            progress('可用内存减少，当前文件改为逐行写入；已签发记录和已有文件保持。')
        checkpoint(cancel)
        from export_files import verify_identity
        progress('核验文件中的补货单号、店仓与签发修订…');verify_identity(temp,identities);checkpoint(cancel)
        # The complete temporary workbook lives on the destination volume.
        # Never remove/truncate the previous signed file before publication.
        try:os.replace(temp,path)
        except PermissionError as exc:
            raise PermissionError('无法写入签发单：'+str(path)+'；如文件正被占用，请关闭 Excel/WPS 后重试，并确认目录有写入权限。已有文件未替换。') from exc
    except BaseException as exc:
        try:exc.resource_budget=resources.stats()
        except Exception:pass
        raise
    finally:
        if os.path.exists(temp):os.unlink(temp)
        if resources.mode!='ample':
            # Closed openpyxl books contain worksheet/parent reference cycles.
            # Reclaim them between files under pressure, before the caller loads
            # the next archive. Live order rows are untouched.
            import gc
            gc.collect()
    return str(path)
