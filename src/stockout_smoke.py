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
            request('adopt',{'stores':[{'code':'S','name':'测试门店S'},{'code':'T','name':'测试门店T'}]})
            run=request('ingest',spec);require(run['state']=='COMPLETE','Same worker projection publishes complete synthetic daily snapshot')
            # Drive the actual acquisition worker over a closed offline protocol
            # fixture, proving that the frozen EXE passes both stores together.
            import copy,shutil
            from aoxiang_jobs import fingerprint
            from stockout_repository import Repository
            native={};native_root=out/'offline-ax';calls=[]
            def native_request(bridge,payload):
                calls.append(copy.deepcopy(payload));service=payload['service']
                if service=='release_ax':return True
                if service=='ax_root':return str(native_root)
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
            require('缺货 6 / 有销 9' in page.total.text(),'Actual page shows weighted numerator and denominator')
            win.resize(1600,1000);page.grab().save(str(out/'01-overview.png'))
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
