"""Optional frozen acceptance scenarios. Only clones of the synthetic input are writable."""
import copy
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from threading import Event

import openpyxl
import data_maintenance as maintenance
import jst_batches as batches
import jst_exports as exports
import jst_orders as orders
import jst_results as results
import lifecycle_io
import operations_flow
import order_lifecycle as lifecycle
import warehouse_catalog as wc
from work_repository import Conflict


def require(value, message):
    if not value:
        raise AssertionError(message)


def save_reply(path, head, source, quantity):
    book=openpyxl.Workbook()
    sheet=book.active
    fields=lifecycle_io.fields('return')
    sheet.append(fields)
    for sku, qty in (('0001',quantity), ('0009',7)):
        row={'补货单号':head['number'],'门店编码':head['store'],'供货仓':head['warehouse'],
             '商品编码':sku,'数量单位':'库存单位','门店确认数量':qty}
        sheet.append([row.get(k) for k in fields])
    book.save(source)
    book.close()
    info=lifecycle_io.inspect([source])[0]
    sheet=info['sheets'][0]
    scan=lifecycle_io.scan('return',[{**info,**sheet,'mapping':{h:i for i,h in enumerate(sheet['headers'])}}])
    lifecycle.commit(path,lifecycle.prepare_numbered(path,scan,head))


def verify(out, source):
    if json.loads((out/'synthetic.json').read_text('utf-8')).get('synthetic') is not True:
        raise ValueError('此验收只接受合成资料')
    wh='河北仓'
    catalog=wc.Repository(source.parent).catalogs()[wh]
    first,second=sorted((h for h in lifecycle.current_targets(source) if h['warehouse']==wh),key=lambda h:h['store'])

    def separate(name):
        folder=out/name
        folder.mkdir()
        path=folder/'workbench.sqlite3'
        with closing(sqlite3.connect(source.resolve().as_uri()+'?mode=ro',uri=True)) as original,closing(sqlite3.connect(path)) as db:
            original.backup(db)
        wc.Repository(folder).save(catalog)
        return path

    path=separate('pending-deletion')
    old=batches.register(path,batches.preview_new(path,[first,second]))
    require([p['summary']['quantity'] for p in old['payload']['plans']]==['40','60'],'原批试算顺序或数量不符')
    maintenance.execute(path,maintenance.prepare(path,[first]))
    try:
        batches.execute(path,old)
    except Conflict as exc:
        require('重新预览' in str(exc),'旧预览拒绝原因错误')
    else:
        raise AssertionError('删除前序后仍放行旧预览')
    r=batches.continue_batch(path,old['payload']['batch_id'])
    after_delete=orders.get(path,r['generated'][0])['summary']['quantity']
    require(after_delete=='80','前序删单后的重预览库存错误')

    path=separate('paid-deletion')
    cancel=Event()
    def stop_after_saved(progress):
        if getattr(progress,'business_event',None):cancel.set()
    partial=batches.execute(path,batches.preview_new(path,[first,second]),progress=stop_after_saved,cancel=cancel)
    require(partial['cancelled'] and len(partial['generated'])==1,'未保留首笔已保存分配')
    lifecycle.commit(path,orders.payment_plan(path,partial['generated'][0]))
    deletion=maintenance.prepare(path,[first])
    require(any('已确认付款' in reason for reason in deletion['external']),'缺少付款影响提示')
    maintenance.execute(path,deletion,True)
    r=batches.continue_batch(path,partial['batch_id'])
    after_paid=orders.get(path,r['generated'][0])['summary']['quantity']
    require(after_paid=='60','删除已付款仓单释放了本批库存')

    path=separate('export-race')
    extra=copy.deepcopy(catalog)
    extra['items']['0009']={**extra['items']['0001'],'商品编码':'0009','基本售价':3,'出库箱规':4}
    extra['revision']=wc.revision(extra['items'])
    wc.Repository(path.parent).save(extra)
    save_reply(path,first,path.parent/'reply-40.xlsx',40)
    operations_flow.adopt(path,[first])
    r=batches.execute(path,batches.preview_new(path,[first]))
    gid=r['generated'][0]
    cache=path.parent/'results.sqlite3'
    frozen=results.export_plan(results.read(path,cache,first['day'],batch_id=r['batch_id']))
    folder=path.parent/'exports'
    original_save=openpyxl.Workbook.save
    new=[]
    def stage(book,target):
        original_save(book,target)
        if new or not Path(target).name.startswith('.jst-'):return
        save_reply(path,first,path.parent/'reply-20.xlsx',20)
        try:
            exports.export_checked(path,[gid],folder)
        except Conflict as exc:
            require('门店回传或运营签发依据已更新' in str(exc),'旧运营依据拒绝原因错误')
        else:
            raise AssertionError('回传变化后仍可导出旧版本')
        operations_flow.adopt(path,[first])
        correction=batches.execute(path,batches.preview_existing(path,r['batch_id'],[first],True))
        new.extend(correction['generated'])
        require(len(new)==1,'新分配未保存')
        require(len(exports.export_checked(path,new,folder)['files'])==1,'新分配未导出')
    openpyxl.Workbook.save=stage
    try:
        rejected=results.export(path,frozen,folder)
    finally:
        openpyxl.Workbook.save=original_save
    require(rejected['remaining']==[gid] and not rejected['files'],'晚到旧文件未被拒绝')
    files=list(folder.glob('*.xlsx'))
    require(len(files)==1 and not list(folder.glob('.jst-*')),'导出遗留正式或临时文件异常')
    book=openpyxl.load_workbook(files[0])
    try:
        rows=list(book.active.values)
        require(book.sheetnames==['订单明细'] and rows==[
            ('商品编码','单价','数量','订单号'),('0001',2,20,first['number']),('0009',3,8,first['number'])],
            '新正式文件的数量、价格或商品顺序变化')
        require(book.active['A2'].data_type=='s' and book.active['A3'].data_type=='s','商品编码类型变化')
    finally:
        book.close()
    saved=orders.get(path,new[0])
    require(saved['summary']['amount']=='64' and saved['catalog']['revision']==extra['revision'],'金额或仓资料版本错误')

    path=separate('resume-restricted')
    pending=batches.register(path,batches.preview_new(path,[first]))
    batches.pause(path,pending['payload']['batch_id'])
    restricted=copy.deepcopy(catalog)
    restricted['items']['0001'].update(商品状态='停采',可用数=8628)
    restricted['revision']=wc.revision(restricted['items'])
    wc.Repository(path.parent).save(restricted)
    resumed=batches.preview_existing(path,pending['payload']['batch_id'])['payload']['plans'][0]
    reason=resumed['rows'][0]['reason']
    require(resumed['catalog']['revision']==catalog['revision'] and resumed['summary']['quantity']=='0'
            and '当前仓资料：该仓商品禁用' in reason,'原批续跑未复核当前停采状态')

    path=separate('zero-then-new')
    empty=copy.deepcopy(catalog)
    empty['items']['0001']['可用数']=0
    empty['revision']=wc.revision(empty['items'])
    zero=batches.execute(path,batches.preview_new(path,[first],{wh:empty}))
    next_batch=batches.execute(path,batches.preview_new(path,[first]))
    original=batches.recover_batch(path,zero['batch_id'])
    require(original['zero']==[first['number']] and not original['generated'] and len(next_batch['generated'])==1,
            '恢复原批时串到新批生成结果')
    return {'deleted_before_generation':after_delete,'deleted_after_confirmed_payment':after_paid,
            'late_export_rejected':rejected['failed'][0]['error'],'excel_rows':rows,
            'saved_amount':saved['summary']['amount'],'catalog_revision':saved['catalog']['revision'],
            'resumed_restriction':reason,'original_batch_zero':original['zero']}
