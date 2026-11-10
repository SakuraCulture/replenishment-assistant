"""Opt-in source/frozen acceptance, real Qt/workers/Excel, fake native transports."""
import json,os,sys,time,traceback,sqlite3,shutil,hashlib
from pathlib import Path
from contextlib import closing
from PySide6.QtCore import QTimer,QDate
from PySide6.QtTest import QTest

def start_smoke(app,win,args):
    out=Path(args[0]).resolve();settings=out/'settings'
    marker=json.loads((out/'synthetic.json').read_text('utf-8'))
    if marker!={'synthetic':True,'live_platform':False} or Path(os.environ.get('REPLENISHMENT_DATA_DIR','')).resolve()!=settings:raise ValueError('仅允许隔离合成资料')
    def execute():
        checks=[];entry=win.warehouse_stockout;original=None;beats=[];timer=QTimer(win);timer.timeout.connect(lambda:beats.append(time.monotonic()));timer.start(25)
        def require(value,name):
            if not value:raise AssertionError(name)
            checks.append(name)
        def wait(predicate,seconds=180):
            deadline=time.monotonic()+seconds
            while not predicate() and time.monotonic()<deadline:QTest.qWait(20)
            if not predicate():raise AssertionError('等待超时：'+entry.message)
        def idle(e):return e.worker is None and e.queued is None
        def request(e,operation,args=None):
            result=[];e.start(operation,args or {},result.append);wait(lambda:idle(e))
            if not result:raise AssertionError(e.message)
            return result[0]
        def signature():
            with closing(sqlite3.connect(win.work_repository.path)) as db:
                result={}
                for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall():
                    if table.startswith('sqlite_'):continue
                    result[table]=hashlib.sha256(repr(db.execute('SELECT * FROM "'+table.replace('"','""')+'" ORDER BY rowid').fetchall()).encode()).hexdigest()
                return result
        try:
            require(entry.page is None and entry.worker is None and entry.timer is None,'大仓板块未进入时不创建页面、工作进程或计划定时器')
            win.jst_generator.timer.stop();wait(lambda:not win.jst_generator.worker)
            before=signature();win.showMinimized();win.hide()
            (out/'single-ready.json').write_text(json.dumps({'pid':os.getpid()}),'utf-8')
            wait(lambda:win.isVisible() and not win.isMinimized(),35);require(True,'再次启动唤醒原隐藏窗口并恢复最小化状态')
            wait(lambda:(out/'second-launch.json').exists(),35);require(json.loads((out/'second-launch.json').read_text())['exit']==0,'第二次启动正常退出，没有第二个业务窗口')
            from stockout_task import Bridge
            original=Bridge.request;native={};rpc_calls=[];cases=json.loads((out/'cases.json').read_text('utf-8'))
            def bridge(self,payload):
                rpc_calls.append(payload['service']+':'+payload.get('action',''))
                if payload['service']=='jst_root':return str(settings/'jst_catalog')
                if payload['service']=='release_jst':return True
                if payload['service']!='jst':raise AssertionError('大仓分析不应请求翱象')
                action=payload['action'];values=payload.get('values',{})
                if action=='sync.start':
                    bid=values['operation_id'];items=[]
                    for i,selected in enumerate(values['items']):
                        ident='item'+str(i);wh=selected['warehouse'];folder=settings/'jst_catalog'/'downloads'/bid/ident;folder.mkdir(parents=True,exist_ok=True);source=folder/'source.xlsx';shutil.copyfile(cases['warehouse'],source)
                        from catalog_sync_validation import validate
                        from stockout_common import file_hash
                        receipt=validate({'schema':1,'warehouse':wh,'file':str(source),'sha256':file_hash(source),'expected_rows':8,'candidate':str(folder/'candidate.json')})
                        items.append({'id':ident,'stage':'VALIDATED','account':{'warehouse':wh,'expectedCoid':'10','expectedUid':'20'},'receipt':receipt,'native':{'scope':{'coid':'10','uid':'20'},'sha256':receipt['source_sha256']}})
                    native[bid]={'id':bid,'items':items};return {'batch_id':bid}
                if action=='sync.status':return {'running':False,'batches':[native[values['batch_id']]],'active':None}
                if action=='sync.acknowledge_adoption':
                    row=next(r for r in native[values['batch_id']]['items'] if r['id']==values['item_id']);row['adoption']=values['receipt'];return True
                if action=='sync.stop':return True
                raise AssertionError('未预期的模拟操作：'+action)
            Bridge.request=bridge
            entry.enter();wait(lambda:idle(entry));request(entry,'select',{'warehouses':['山东仓']});result=request(entry,'collect',{'warehouses':['山东仓']});rid=result['run']
            from stockout_common import today
            options={'start':today(),'end':today()};total=request(entry,'query',options)
            require((total['base'],total['zero'],total['excluded'],total['rate'])==(5,2,3,.4),'独立大仓采集：负库存纳入缺货，三类剔除正确')
            cuts=request(entry,'cuts',{'options':options,'dimensions':['warehouse','supplier','category']});require('supplier_code' in cuts['dimensions'] and sum(r['base'] for r in cuts['rows'])==5,'自定义多维切面同时保留供应商编码与名称')
            filtered=request(entry,'query',{**options,'filters':{'supplier':['供货一'],'category':['饮料']}});require(filtered['rate']==.5,'同维多选、跨维交叉筛选结果正确')
            exported=out/'warehouse-report.xlsx';request(entry,'export',{'options':options,'destination':str(exported),'combinations':[{'name':'大仓供应商分类','dimensions':['warehouse','supplier','category']}],'include_detail':True})
            import openpyxl
            b=openpyxl.load_workbook(exported,read_only=True,data_only=True)
            try:require(len(list(b['商品明细1'].values))==9,'Excel导出包含全部八款商品及剔除原因，编码作为文本保留')
            finally:b.close()
            count=len(rpc_calls);recalculated=request(entry,'recalculate',{'run':rid});require(len(rpc_calls)==count,'大仓原件重算不访问平台');require(request(entry,'query',options)['rate']==.4,'大仓重算结果等价')
            entry.page.refresh_metadata();wait(lambda:idle(entry));win.resize(1500,950);entry.page.tabs.setCurrentIndex(1);wait(lambda:idle(entry));win.grab().save(str(out/'warehouse-ui.png'))
            plan=request(entry,'delete_preview',options);request(entry,'cleanup',{'plan':plan});require(request(entry,'query',options)['total']==0,'删除范围内大仓数据并回收无引用原件')
            require(signature()==before,'大仓采集、重算、删除均未改动补货业务表')
            win.show_section(0);wait(lambda:idle(entry));require(entry.page is None and entry.watch is None,'离开板块释放页面和查询资源')
            store=win.stockout;store.enter();wait(lambda:idle(store));page=store.page
            page.start.setDate(QDate(2026,11,7));page.end.setDate(QDate(2026,11,7));page.refresh();wait(lambda:idle(store))
            values={r['sku']:r['expected_sales_days'] for r in page.detail_rows};require(values=={'normal':15,'negative':-1,'zero':None},'门店预计销售天数：正常、负库存、零月销显示一致')
            from PySide6.QtWidgets import QTabWidget,QApplication
            page.findChild(QTabWidget).setCurrentIndex(2);win.grab().save(str(out/'store-days-ui.png'))
            storefile=out/'store-report.xlsx';request(store,'export',{'query':page.options(),'destination':str(storefile),'include_detail':True});require(storefile.is_file(),'门店报表包含预计销售天数与缺失原因')
            win.show_section(0);wait(lambda:idle(store))
            from business_ui import run
            import history_read,archive_exports,order_migration,parameter_snapshot
            heads=history_read.execute(win.work_repository.path,out/'qa-history.sqlite3',{'kind':'list'},__import__('threading').Event(),lambda _:None)['records']
            calc=out/'calculation.xlsx';run(win,archive_exports.export,(win.work_repository.path,heads[0],calc),'验收历史业务计算表')
            b=openpyxl.load_workbook(calc,read_only=True,data_only=True)
            try:require(b.sheetnames==['完整计算建议表','字段与口径说明'],'完整计算表使用两张业务工作表');rows=list(b.worksheets[0].values);require(len(rows[0])<125 and '供货仓可用库存' in rows[0] and '商品编码' in rows[0],'计算表为固定可分析字段，不展开技术树')
            finally:b.close()
            param=out/'parameters.xlsx';run(win,parameter_snapshot.export,(win.work_repository,param),'验收完整参数导出');require(param.is_file(),'全部门店参数和预案成功导出')
            before=signature();package=out/'orders.zip';result=run(win,order_migration.export,(settings,package),'验收完整订单迁移导出');require(result['manifest']['counts']['normal_orders']==1,'迁移包包含全部订单与修订，不受当前筛选限制')
            target=out/'restore-preview';target.mkdir();prepared=run(win,order_migration.prepare,(target,package),'验收迁移导入完整性预览');require(not list(target.iterdir()),'导入预览前不写目标业务资料');order_migration.discard(prepared);require(signature()==before,'导出与预览均未修改订单业务事实')
            from order_migration_ui import open_migration
            captured=[]
            def capture():
                modal=QApplication.activeModalWidget()
                if modal:captured.append(modal.windowTitle());modal.grab().save(str(out/'migration-ui.png'));modal.reject()
            QTimer.singleShot(300,capture);open_migration(win,win);require(captured==['完整订单迁移与参数导出'],'订单跟踪内提供完整迁移、导入与参数导出入口')
            require(not any(k.startswith('ax:') for k in rpc_calls),'大仓分析完全不调用翱象')
            from version_info import diagnostic
            report={'passed':True,**diagnostic(),'frozen_exe':bool(getattr(sys,'frozen',False)),'pid':os.getpid(),'checks':checks,'max_heartbeat_gap_seconds':max((b-a for a,b in zip(beats,beats[1:])),default=0),
                'limits':'Synthetic only; real Qt, child workers and Excel. Native JST replies simulated. Import preview tested in EXE; replacement/rollback covered by isolated source tests. No live platform or low-spec PC test.'}
        except Exception:report={'passed':False,'checks':checks,'error':traceback.format_exc(),'frozen_exe':bool(getattr(sys,'frozen',False))}
        finally:
            for e in (win.stockout,entry):
                if e.worker:e.pause();wait(lambda:idle(e))
            if original:
                from stockout_task import Bridge
                Bridge.request=original
            timer.stop()
        if report['passed']:
            try:
                # Exercise the actual shutdown-only restore path. The exact
                # marked data root was checked above; no production folder is
                # used, and the ordinary process/rename guards remain enabled.
                from business_ui import run
                import order_migration
                plan=run(win,order_migration.prepare,(settings,out/'orders.zip'),'验收退出后整组迁入')
                expected={k:v for k,v in signature().items() if k in ('normal_orders','normal_revisions','normal_work_revisions','lifecycle_versions','jst_generations')}
                win._order_import_request=plan
                acknowledgement=QTimer(app);app._operations_restore_ack=acknowledgement
                def acknowledge():
                    from PySide6.QtWidgets import QApplication
                    modal=QApplication.activeModalWidget()
                    if modal is None or modal.windowTitle() not in ('订单已迁入','订单迁入未完成'):return
                    try:
                        if modal.windowTitle()!='订单已迁入':raise AssertionError(modal.text())
                        receipt=json.loads((settings/'订单迁入完成.json').read_text('utf-8'))
                        if receipt['nonce']!=plan['nonce'] or not Path(plan['backup']).is_dir():raise AssertionError('迁入回执或完整备份缺失')
                        with closing(sqlite3.connect(settings/'workbench.sqlite3')) as db:
                            for table,h in expected.items():
                                actual=hashlib.sha256(repr(db.execute('SELECT * FROM '+table+' ORDER BY rowid').fetchall()).encode()).hexdigest()
                                if actual!=h:raise AssertionError('迁入后业务事实变化：'+table)
                        report['checks'].append('关闭EXE后实际整组迁入，完整备份保留，签发及生命周期事实一致')
                        report['restore_backup']=plan['backup'];report['restore_receipt']=receipt
                        report['limits']='Synthetic only; real Qt, child workers, Excel and shutdown restore. Native JST replies simulated. Failure rollback covered by isolated source tests. No live platform or low-spec PC test.'
                    except Exception:report.update(passed=False,error=traceback.format_exc())
                    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8');acknowledgement.stop();modal.accept()
                acknowledgement.timeout.connect(acknowledge);acknowledgement.start(100)
            except Exception:report.update(passed=False,error=traceback.format_exc())
        (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8');win._discard_close=True;win.job_dirty=False;win.close()
    QTimer.singleShot(600,execute)
