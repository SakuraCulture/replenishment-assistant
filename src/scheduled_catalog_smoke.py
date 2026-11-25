"""The frozen EXE exercises the catalogue change between 25 and 29 ready stores."""
import copy
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from unittest.mock import patch


def save_catalog_file(path,stock,pack):
    from openpyxl import Workbook
    import warehouse_catalog as wc
    book=Workbook();sheet=book.active
    sheet.append(wc.REQUIRED+['商品标签','商品名称'])
    sheet.append(['0001','饮品',2,pack,'启用',stock,'','合成饮品'])
    book.save(path);book.close()


def inventory_fixture(path,root,result,day,catalogs):
    import jst_batches as batches, normal_orders as normal, order_lifecycle as lc
    import operations_flow, result_semantics, work_repository as wr
    from jst_batch_consistency_smoke import save_reply
    # These archived synthetic orders are fixtures, separate from the 29 runs.
    with closing(sqlite3.connect(path)) as db,db:
        for store in ('INVENTORY1','INVENTORY2'):
            item=copy.deepcopy(result['rows'][0]);item['B']=store
            rows=[{**copy.deepcopy(item),'AU':wh} for wh in ('河北仓','山东仓')]
            r={**copy.deepcopy(result),'store':store,'store_name':store,'rows':rows,'summary':result_semantics.summarize(rows)}
            oid='catalog-check-'+store
            db.execute('INSERT INTO normal_orders VALUES(?,?,?,?,?,?)',(day,store,'normal_model',oid,'BH-'+store+'-ZC',1))
            db.execute('INSERT INTO normal_revisions VALUES(?,?,?,?,?)',(oid,1,normal.fingerprint(r),wr.now(),
                normal.encode({'schema':1,'result':r,'work_revision':'stock-smoke','new_focus':None},db)))
        db.execute('INSERT INTO normal_work_revisions VALUES(?,?,?)',('stock-smoke',day,normal.encode({'catalogs':catalogs},db)))
    heads=lc.current_targets(path)
    first=[h for h in heads if h['store']=='INVENTORY1'];pending=[h for h in heads if h['store']=='INVENTORY2']
    assert len(first)==len(pending)==2
    for h in [*first,*pending]:save_reply(path,h,root/(h['number']+'.xlsx'),36)
    operations_flow.adopt(path,[*first,*pending])
    original=batches.preview_new(path,first)
    receipt=batches.execute(path,original,True)
    assert len(receipt['generated'])==2 and not receipt['errors'],receipt
    return original,receipt,batches.preview_new(path,pending)


def occupancy(path):
    with closing(sqlite3.connect(path)) as db:
        return {'reservations':db.execute('SELECT * FROM jst_reservations ORDER BY gid,sku').fetchall(),
                'active':db.execute('SELECT * FROM jst_active ORDER BY oid,warehouse').fetchall(),
                'events':db.execute("SELECT id,oid,warehouse,action,detail FROM jst_events WHERE oid IS NOT NULL AND oid<>'' ORDER BY id").fetchall()}


def refresh_in_fixture(app,win,dialog,out,old,result,wait,require):
    import jst_batches as batches,jst_orders as orders
    from scheduled_replenishment import stamp
    from warehouse_catalog import Repository
    from work_repository import Conflict
    path=win.work_repository.path;master=Repository(path.parent)
    original,receipt,stale=inventory_fixture(path,out,result,stamp().date().isoformat(),old)
    ledger=occupancy(path)
    require(len(ledger['reservations'])==2,'同店双仓同码在原仓单分别建立库存占用')
    fresh=out/'山东仓60件_V2.xlsx';save_catalog_file(fresh,60,10)
    begin=time.monotonic()
    dialog.hide()
    with patch('import_ui.choose',return_value=[str(fresh)]), patch('interaction_ui.confirm',return_value=True):
        win.show_section(1);win.catalog_buttons['山东仓'].click()
        wait(lambda:(master.snapshots(('山东仓',))[0].get('山东仓',{}).get('items',{}).get('0001',{}).get('可用数')==60 and not win.worker))
    dialog.show();app.processEvents()
    refresh_seconds=time.monotonic()-begin
    require(occupancy(path)==ledger,'山东仓刷新，原仓单占用、活动生成单及事件一致')
    try:batches.register(path,stale)
    except Conflict as error:
        require('山东仓' in str(error) and '重新预览' in str(error),'V1旧预览登记时指出需重预览的山东仓')
    else:raise AssertionError('旧库存预览被误放行')
    require(occupancy(path)==ledger,'过期预览未产生占用或释放')
    repeated=batches.execute(path,original,True)
    require(repeated['generated']==receipt['generated'] and occupancy(path)==ledger,'已登记分配批重试仍是同两张原仓单、同有效占用')
    catalog=master.catalogs()
    require(catalog['河北仓']==old['河北仓'],'只刷新山东，河北仓原版及原件一致')
    allocation=[]
    for gid in receipt['generated']:
        p=orders.get(path,gid)
        allocation.append({'head':p['head'],'batch':p['batch_id'],'gid':gid,'revision':p['catalog']['revision'],
                           'rows':[{k:r[k] for k in ('sku','stock','before','quantity','after')} for r in p['rows']]})
    return {'new_catalogs':catalog,'allocation':allocation,'before':ledger,'after':occupancy(path),
            'refresh_seconds':refresh_seconds,'occupations':0,'releases':0,'replayed_generations':repeated['generated']}


def finish_scenario(win,dialog,rid,selection,old,bindings,evidence,computations,pick_run,wait,require,out):
    import scheduled_execution as execution, scheduled_replenishment as sr
    from scheduled_smoke import SyntheticPlatforms
    from warehouse_catalog import Repository
    from work_repository import Conflict
    from PySide6.QtWidgets import QPushButton
    m=win.scheduled;repo=m.repo
    start_trace=copy.deepcopy(computations)
    finished=[code for group in start_trace for code in group]
    codes=[item['code'] for item in selection['stores']]
    require(finished==codes[:25]+codes[-3:]+codes[-4:-3],'原批继续只计算后4家，各店只计算一次')
    require(repo.value(rid,'catalogs')==old,'原批29店计算始终读取山东100、河北175的完整旧资料')
    manifest=repo.value(rid,'sales')
    fresh=evidence.pop('new_catalogs')
    def synthetic(catalogs):return lambda cancel:SyntheticPlatforms(repo,catalogs,manifest,cancel,bindings)
    def run_reference(catalogs):
        m.platform=synthetic(catalogs);reference=repo.manual({**selection,'workers':1})
        m.start(reference)
        wait(lambda:not m.worker and not m.reader and repo.get(reference['id'])['state'] not in sr.ACTIVE,90)
        require(repo.get(reference['id'])['state']=='COMPLETE','29家串行基线完整计算')
        return reference['id']
    old_serial=run_reference(old);new_serial=run_reference(fresh)
    m.platform=synthetic(old) # This client would still return the old warehouse file.
    pick_run()
    next(b for b in dialog.findChildren(QPushButton) if b.text()=='按新资料另跑').click()
    def new_run():return next((r for r in repo.list()['items'] if r.get('repeated_from')==rid),None)
    wait(lambda:new_run() and new_run()['state'] not in sr.ACTIVE and not m.worker and not m.reader,90)
    new=new_run()['id']
    require(new_run()['state']=='COMPLETE' and repo.value(new,'catalogs')==fresh,'B批走新入口，明确冻结山东60件、箱规10和原河北')
    root=win.work_repository.path.parent
    try:execution.publish_catalogs(repo,rid,root)
    except Conflict:
        require(repo.get(rid)['publication']['state']=='NEEDS_REVIEW','晚于V2发布的旧原批留存冲突和计算结果')
    else:raise AssertionError('原批旧资料被再次发布')
    require(Repository(root).catalogs()==fresh,'晚发布原批后山东默认仍是V2，河北默认一致')
    rows=[]
    def outcome(run,store):
        r=repo.value(run,'result:'+store)
        return {k:r[k] for k in ('rows','summary','warehouses','config','supply_route')}
    for code in codes:
        A=outcome(rid,code);B=outcome(new,code)
        require(A==outcome(old_serial,code) and B==outcome(new_serial,code),code+'原批及新批分别与同版本串行数量、金额、字段和行序对账')
        wh=A['supply_route']['primary']
        if wh=='河北仓':require(A==B,code+'河北相同SKU采用和计算结果一致')
        row={'store':code,'warehouse':wh,'original_run':rid,'new_run':new,
             'original_revision':old[wh]['revision'],'new_revision':fresh[wh]['revision'],
             'original_source_sha256':old[wh]['source_sha256'],'new_source_sha256':fresh[wh]['source_sha256'],
             'original_stock':old[wh]['items']['0001']['可用数'],'new_stock':fresh[wh]['items']['0001']['可用数'],
             'original_total':A['summary'],'new_total':B['summary'],
             'original_result_sha256':sr.signature(A),'new_result_sha256':sr.signature(B),
             'resume_calculations':int(code in codes[-4:])}
        rows.append(row)
    (out/'逐店对账.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2),'utf-8')
    return {**evidence,'A':rid,'B':new,'serial_V1':old_serial,'serial_V2':new_serial,'original_compute_calls':start_trace,
            'default_after_late_publish':{w:data['revision'] for w,data in fresh.items()},'store_rows':len(rows)}
