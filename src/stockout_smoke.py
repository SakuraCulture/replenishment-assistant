"""Actual source/frozen Qt acceptance. Marked synthetic inputs; no live platform RPC."""
import json,os,sys,time,traceback,sqlite3,hashlib
from pathlib import Path
from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest


def start_smoke(app,win,args):
    out=Path(args[0]).resolve();settings=out/'settings'
    if not (out/'synthetic.json').is_file() or Path(os.environ.get('REPLENISHMENT_DATA_DIR','')).resolve()!=settings:
        raise ValueError('Synthetic isolation required')
    def execute():
        import logging
        logging.getLogger().setLevel(logging.DEBUG)
        checks=[];diagnostics=[];beats=[];entry=win.stockout
        timer=QTimer(win);timer.setInterval(25);timer.timeout.connect(lambda:beats.append(time.monotonic()));timer.start()
        def require(value,name):
            if not value:raise AssertionError(name)
            checks.append(name)
        def wait(predicate,seconds=180):
            end=time.monotonic()+seconds
            while not predicate() and time.monotonic()<end:QTest.qWait(10)
            if not predicate():raise AssertionError('Timeout: '+entry.message)
        def idle():return entry.worker is None and entry.queued is None
        def request(operation,arguments=None):
            results=[];entry.start(operation,arguments or {},results.append);task=entry.worker
            wait(idle)
            if task and task.diagnostic:diagnostics.append(task.diagnostic)
            if not results:raise AssertionError(entry.message)
            return results[0]
        def business():
            db=sqlite3.connect(Path(win.work_repository.path).as_uri()+'?mode=ro',uri=True)
            try:
                values={}
                for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall():
                    quoted='"'+name.replace('"','""')+'"';h=hashlib.sha256()
                    for row in db.execute('SELECT * FROM '+quoted+' ORDER BY rowid'):h.update(repr(row).encode())
                    values[name]=h.hexdigest()
                return values
            finally:db.close()
        try:
            require(entry.page is None and entry.worker is None and entry.timer is None,'Unentered page has no page, worker or schedule timer')
            require(not (settings/'analysis').exists(),'No analysis directory or database on ordinary startup')
            require(not any(k in sys.modules for k in ('stockout_ui','stockout_metrics','stockout_repository','stockout_facts','stockout_task')),'No heavy analysis imports before entry')
            before=business();originals={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in [settings/'store_supply.json',settings/'must_sell.json']}
            original_begin=entry.begin
            def begin(request):
                original_begin(request);task=entry.worker
                task.finished.connect(lambda:diagnostics.append(task.diagnostic) if task.diagnostic else None)
            entry.begin=begin
            # Deliberately reject every bridge request; this acceptance cannot reach a real platform.
            import stockout_task
            original_request=stockout_task.Bridge.request
            forbidden=lambda *a,**k:(_ for _ in ()).throw(AssertionError('Live RPC forbidden in synthetic smoke'))
            stockout_task.Bridge.request=forbidden
            entry.enter();wait(idle)
            require(entry.page is not None and (settings/'analysis/stockout/stockout.sqlite3').is_file(),'First entry lazily creates independent database')
            spec=json.loads((out/'ingest.json').read_text('utf-8'))
            adopted=request('adopt',{'stores':[{'code':'S','name':'测试门店S'},{'code':'T','name':'测试门店T'}]})
            require(adopted['route_source']['source']=='参数设置 / 已保存门店配置' and len(adopted['routes'])==2,
                    'Analysis adopts supply routes from the saved parameter-page database')
            run=request('ingest',spec);require(run['state']=='COMPLETE','Same worker projection publishes complete synthetic daily snapshot')
            # Drive the actual acquisition worker over a closed offline protocol
            # fixture, proving that the frozen EXE passes both stores together.
            import copy,shutil
            from aoxiang_jobs import fingerprint
            from stockout_repository import Repository
            native={};native_root=out/'offline-ax';calls=[];jst_batches={};jst_root=out/'offline-jst'
            def native_request(bridge,payload):
                calls.append(copy.deepcopy(payload));service=payload['service']
                if service in {'release_ax','release_jst'}:return True
                if service=='ax_root':return str(native_root)
                if service=='jst_root':return str(jst_root)
                if service=='jst':
                    action=payload['action'];values=payload.get('values',{})
                    if action=='sync.start':
                        bid=values['operation_id'];assert values['purpose']=='stockout_analysis'
                        if bid not in jst_batches:
                            from catalog_sync_validation import validate
                            folder=jst_root/'downloads'/bid/'item';folder.mkdir(parents=True)
                            spec_cat=spec['catalogs']['山东仓'];source=folder/'source.xlsx';shutil.copyfile(spec_cat['file'],source)
                            receipt=validate({'schema':1,'file':str(source),'warehouse':'山东仓','sha256':spec_cat['sha256'],
                                              'expected_rows':spec_cat['rows'],'candidate':str(folder/'candidate.json')})
                            jst_batches[bid]={'id':bid,'operation_id':bid,'purpose':'stockout_analysis','items':[
                                {'id':'item','stage':'READY','account':{'warehouse':'山东仓','expectedCoid':'C','expectedUid':'U'},
                                 'native':{'scope':{'coid':'C','uid':'U'},'sha256':receipt['source_sha256']},'receipt':receipt,'adoption':None}]}
                        return {'batch_id':bid,'reused':False}
                    if action=='sync.status':return {'running':False,'batches':[copy.deepcopy(jst_batches[values['batch_id']])]}
                    if action=='sync.acknowledge_adoption':jst_batches[values['batch_id']]['items'][0]['adoption']=values['receipt'];return {'accepted':True}
                    raise AssertionError('Unexpected offline JST action: '+action)
                if service!='ax':raise AssertionError('Live platform service forbidden: '+service)
                action=payload['action'];values=payload.get('values',{})
                if action=='configuration':return {'configured':True,'enabled':True,'account_binding':{'account_id':'offline','merchant_code':'QA'},
                    'store_mapping_version':'offline-v1','targets':[{'buyerWarehouseCode':s,'displayName':'测试'+s} for s in ('S','T')]}
                if action=='reserve':
                    command=values['command'];cid=command['command_id']
                    assert command['operation']=='replenishment.export'
                    if cid not in native:
                        folder=native_root/'command-files'/cid;folder.mkdir(parents=True);items=[]
                        for index,code in enumerate(command['scope']['warehouse_codes']):
                            file=next(f for f in spec['files'] if f['store']==code)
                            target=folder/(str(index)+'-'+file['sha256']+'.xlsx');shutil.copyfile(file['file'],target)
                            identity='offline-'+code
                            result={'warehouse_code':code,'run_id':identity,'file':str(target),'sha256':file['sha256'],'rows':file['rows'],
                                    'bytes':target.stat().st_size,'account_binding':command['account_binding'],'collected_at':file['observed']}
                            items.append({'index':index,'warehouse_code':code,'run_id':identity,'task_id':identity,
                                          'state':'SUCCEEDED','result':result,'error':None})
                        native[cid]={'id':cid,'request':command,'fingerprint':fingerprint(command),'sequence':1,'state':'SUCCEEDED','items':items}
                    return {'record':copy.deepcopy(native[cid])}
                if action=='status':return copy.deepcopy(native[values['command_id']])
                raise AssertionError('Unexpected offline action: '+action)
            repo=Repository(settings);batch=repo.new_run()
            repo.change(batch['id'],catalogs=run['catalogs'],external={'catalogs_frozen':True})
            stockout_task.Bridge.request=native_request
            collected=request('collect',{'resume':batch['id']})
            stockout_task.Bridge.request=forbidden
            require(collected['state']=='COMPLETE','Actual acquisition worker publishes all selected stores')
            require(len(native)==1 and next(iter(native.values()))['request']['scope']['warehouse_codes']==['S','T'],
                    'All selected stores use one native batch command in their selected order')
            require(len(set(collected['external']['ax_commands'].values()))==1,'Analysis persists the shared command association for every store')
            entry.page.refresh_metadata();wait(idle);page=entry.page
            page.start.setDate(page.end.date());page.refresh();wait(idle)
            require(page.exclude_stopped.isChecked() and page.exclude_disabled.isChecked()
                    and '缺货 5 / 有销 8' in page.total.text(),'Default view excludes stopped and disabled items from both numerator and denominator')
            page.exclude_stopped.setChecked(False);page.exclude_disabled.setChecked(False);page.refresh();wait(idle)
            require('缺货 6 / 有销 9' in page.total.text(),'Actual page shows weighted numerator and denominator')
            require([page.cuts.horizontalHeaderItem(i).text() for i in range(2)]==['门店编码','门店名称']
                    and all('测试门店' in page.cuts.item(i,1).text() for i in range(page.cuts.rowCount())),
                    'Store cuts display both exact code and saved snapshot name')
            require([page.details.horizontalHeaderItem(i).text() for i in range(1,5)]==['门店编码','门店名称','商品编码','商品名称'],
                    'Details display both store and product code and name')
            win.resize(1600,1000);page.grab().save(str(out/'01-overview.png'))
            page.dimension.setCurrentIndex(page.dimension.findData('sku'));page.second.setCurrentIndex(page.second.findData('store'));page.refresh();wait(idle)
            require([page.cuts.horizontalHeaderItem(i).text() for i in range(4)]==['商品编码','商品名称','门店编码','门店名称']
                    and '测试商品' in page.cuts.item(0,1).text(),'Product and store cross-cut includes both identities')
            selected=page.cut_rows[0];page.select_cut(0,0);wait(idle)
            require(page.filters['sku']==[selected['sku']] and page.filters['store']==[selected['store']]
                    and selected['sku_name'] in page.filter_note.text() and selected['store_name'] in page.filter_note.text(),
                    'Readable filter labels preserve exact code-only selection')
            page.clear_filters();wait(idle)
            from PySide6.QtWidgets import QApplication,QDialog,QTabWidget
            page.findChild(QTabWidget).setCurrentIndex(1);QTest.qWait(100);page.grab().save(str(out/'03-readable-cut.png'))
            def inspect_supply():
                dialog=QApplication.activeModalWidget()
                if isinstance(dialog,QDialog):dialog.grab().save(str(out/'04-adopted-supply.png'));dialog.reject()
            QTimer.singleShot(100,inspect_supply);page.show_supply()
            for key,expected in [('all',(9,6)),('retired',(7,4)),('assessment',(7,5)),('both',(6,4))]:
                page.view.setCurrentIndex(page.view.findData(key));page.refresh();wait(idle)
                require(f'缺货 {expected[1]} / 有销 {expected[0]}' in page.total.text(),'View '+key+' updates numerator and denominator')
            page.dimension.setCurrentIndex(page.dimension.findData('source'));page.second.setCurrentIndex(page.second.findData('mandatory'));page.refresh();wait(idle)
            require(sum(r['base'] for r in page.cut_rows)==6,'Source and mandatory cross-cut reconciles with total')
            page.select_cut(0,0);wait(idle);require(bool(page.filters),'Double-click cut applies exact filters');page.clear_filters();wait(idle)
            page.grab().save(str(out/'02-filter.png'))
            exported=request('export',{'query':page.options(),'destination':str(out/'report.xlsx'),'include_detail':True,'dimensions':['source','mandatory']})
            require(Path(exported).is_file(),'Actual worker exports independent detailed workbook')
            import openpyxl
            book=openpyxl.load_workbook(exported,read_only=True)
            try:require(list(book['总体'].values)[1][:2]==(6,4),'Workbook and actual page agree')
            finally:book.close()
            page.view.setCurrentIndex(page.view.findData('all'));page.exclude_stopped.setChecked(True);page.exclude_disabled.setChecked(True);page.refresh();wait(idle)
            require('缺货 5 / 有销 8' in page.total.text() and all(r['sku']!='C' for r in page.detail_rows),
                    'Status filters apply to current cards and product details')
            page.grab().save(str(out/'05-status-filters.png'))
            status_export=request('export',{'query':page.options(),'destination':str(out/'status-report.xlsx'),'include_detail':True})
            book=openpyxl.load_workbook(status_export,read_only=True)
            try:require(list(book['总体'].values)[1][:2]==(8,5),'Status-filtered export equals visible totals')
            finally:book.close()
            # Exercise the actual action buttons, confirmation callbacks and spawned workers.
            from unittest.mock import patch
            from PySide6.QtWidgets import QMessageBox,QInputDialog
            page.findChild(QTabWidget).setCurrentIndex(3);wait(idle)
            page.runs.setCurrentIndex(page.runs.findData(collected['id']));wait(idle)
            stockout_task.Bridge.request=native_request
            with patch.object(QMessageBox,'question',return_value=QMessageBox.StandardButton.Yes):
                page.rerun_btn.click();wait(idle)
            stockout_task.Bridge.request=forbidden
            fresh=repo.run(repo.recent()[0]['id'])
            require(fresh['trigger']=='rerun' and fresh['state']=='COMPLETE' and len(native)==2,
                    'Rerun button creates a fresh successful native collection with both stores')
            require(fresh['external']['ax_commands']!=collected['external']['ax_commands'] and len(jst_batches)==1,
                    'Explicit rerun gets new platform command and warehouse receipt without changing original task')
            page.runs.setCurrentIndex(page.runs.findData(fresh['id']));wait(idle)
            with patch.object(QInputDialog,'getItem',return_value=('原作业当时设置',True)),patch.object(QMessageBox,'question',return_value=QMessageBox.StandardButton.Yes):
                page.recalculate_btn.click();wait(idle)
            recalculated=repo.run(repo.recent()[0]['id'])
            require(recalculated['trigger']=='recalculate' and recalculated['state']=='COMPLETE',
                    'Recalculate button publishes new revision with live RPC forbidden')
            require(recalculated['stores'][0]['snapshot']!=fresh['stores'][0]['snapshot'] and '缺货 5 / 有销 8' in page.total.text(),
                    'Recalculation creates fresh snapshots and preserves original metric')
            page.runs.setCurrentIndex(page.runs.findData(recalculated['id']));wait(idle);page.grab().save(str(out/'06-lifecycle.png'))
            with patch.object(QMessageBox,'question',return_value=QMessageBox.StandardButton.Yes),patch.object(QMessageBox,'information'):
                page.delete_log_btn.click();wait(idle)
            require(recalculated['id'] not in [r['id'] for r in repo.recent()] and '缺货 5 / 有销 8' in page.total.text(),
                    'Delete-log button removes task from list while preserving current analysis')
            page.result_links.setChecked(True);wait(idle)
            require(page.runs.findData(recalculated['id'])>=0 and repo.run(recalculated['id'])['external'].get('log_deleted_at'),
                    'Deleted-log result links remain available for future recalculation without restoring logs')
            with patch.object(QMessageBox,'warning',return_value=QMessageBox.StandardButton.Cancel):
                page.delete_data_btn.click();wait(idle)
            require('缺货 5 / 有销 8' in page.total.text() and page.details.rowCount()>0,
                    'Cancelling deletion confirmation preserves analysis results')
            with patch.object(QMessageBox,'warning',return_value=QMessageBox.StandardButton.Yes),patch.object(QMessageBox,'information'):
                page.delete_data_btn.click();wait(idle)
            require('缺货 0 / 有销 0' in page.total.text() and page.details.rowCount()==0,
                    'Date-scoped data deletion refreshes empty state with no stale results')
            require(all(Path(f['file']).is_file() for f in spec['files']) and len(native)==2,
                    'Analysis data deletion preserves input originals and platform command records')
            for n in range(3):
                page.refresh();win.show_section(0);wait(idle)
                require(entry.page is None and entry.callback is None,'Leave cancels query and releases page '+str(n))
                QTest.qWait(100);entry.enter();wait(idle);page=entry.page
            require(all(not d['qt_loaded'] for d in diagnostics),'Spawned workers never create/import Qt application')
            require(business()==before,'All original business tables retain exact ordered signatures')
            require(all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==h for p,h in originals.items()),'Original route and mandatory profile bytes unchanged')
            require(entry.timer is None,'Entering and collecting do not enable daily schedule')
            win.show_section(0);wait(idle);QTest.qWait(350)
            require(entry.worker is None and entry.watch is None,'No worker or polling remains after leaving')
            # Corruption remains confined to the newly created test analysis database.
            db=settings/'analysis/stockout/stockout.sqlite3';backup=db.with_suffix('.verified');db.rename(backup);db.write_bytes(b'isolated corruption fixture')
            entry.enter();wait(idle);require('分析未完成' in entry.message,'Corrupt analysis database produces an analysis error')
            win.show_section(0);wait(idle);db.unlink();backup.rename(db)
            require(business()==before,'Original business remains available after analysis corruption')
            stockout_task.Bridge.request=original_request
            from version_info import diagnostic
            result={'passed':True,**diagnostic(),'application_pid':os.getpid(),'frozen_exe':bool(getattr(sys,'frozen',False)),'checks':checks,'workers':diagnostics,
                    'pipeline_scopes':[r['request']['scope'] for r in native.values()],
                    'business_signatures':before,'heartbeat_ticks':len(beats),'max_heartbeat_gap_seconds':max((b-a for a,b in zip(beats,beats[1:])),default=0),
                    'limits':'Marked synthetic originals; real Qt, spawned worker and Excel. No live platform requests. Not a low-spec business-PC measurement.'}
        except Exception:result={'passed':False,'checks':checks,'error':traceback.format_exc(),'frozen_exe':bool(getattr(sys,'frozen',False))}
        finally:
            if entry.worker:entry.pause();wait(idle)
            timer.stop()
        (out/'report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),'utf-8')
        win._discard_close=True;win.job_dirty=False;app.exit(0 if result['passed'] else 1)
    QTimer.singleShot(400,execute)
