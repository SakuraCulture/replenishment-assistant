"""Opt-in actual generation-panel acceptance; isolated prepared SQLite only."""
import json,os,sys,time,traceback,sqlite3
from pathlib import Path
from contextlib import closing
from PySide6.QtCore import QTimer,Qt
from PySide6.QtWidgets import QMessageBox
from PySide6.QtTest import QTest

def start_smoke(app,win,args):
    out=Path(args[0]).resolve();checks=[];messages=[];ticks=[];dialog=None
    reopening='reopen' in args[1:];comparison={}
    win.auto_check.stop();win.jst_generator.stopped=True;win.jst_generator.timer.stop();win.aoxiang_workflows.stopping=True;win.aoxiang_workflows.timer.stop()
    def tick():
        ticks.append(time.monotonic());box=app.activeModalWidget()
        if isinstance(box,QMessageBox):
            messages.append({'title':box.windowTitle(),'text':box.text()})
            if box.windowTitle() in ('确认生成聚水潭订单','确认更新并保存仓资料'):
                box.button(QMessageBox.StandardButton.Yes).click()
            else:box.accept()
    timer=QTimer(win);timer.setInterval(25);timer.timeout.connect(tick)
    def wait(fn):
        deadline=time.monotonic()+45
        while not fn() and time.monotonic()<deadline:QTest.qWait(20)
        if not fn():raise AssertionError('QA界面任务未安全结束')
    def require(ok,text):
        if not ok:raise AssertionError(text)
    def execute():
        nonlocal dialog
        timer.start()
        try:
            import warehouse_catalog as wc,jst_orders as orders,jst_batches as batches,order_lifecycle as lc,export_settings,export_locations,openpyxl
            from jst_ui import GenerationDialog
            path=win.work_repository.path
            require(Path(os.environ.get('REPLENISHMENT_DATA_DIR','')).resolve()==out/'settings' and path.resolve()==out/'settings/workbench.sqlite3','必须使用隔离数据目录')
            require(json.loads((out/'synthetic.json').read_text('utf-8'))['synthetic'] is True,'必须使用合成夹具')
            folder=out/'exports';folder.mkdir(exist_ok=True);export_settings.save(export_locations.config_path(win),str(folder))
            heads_by_store={h['store']:h for h in lc.current_targets(path) if h['warehouse']=='河北仓'}
            heads=[heads_by_store[store] for store in ('PRE1','PRE2','T')];target=heads[-1]
            repo=wc.Repository(path.parent)
            def stock(catalog):return catalog['items']['0001']['可用数']
            if reopening:
                with closing(sqlite3.connect(path)) as db:
                    gid=orders.active(db,target);batch_id=batches.generation_batch(db,gid)
                    frozen=batches.context(db,batch_id)['stocks']['河北仓']['catalog']
                require(stock(frozen)==8628 and orders.get(path,gid)['summary']['quantity']=='360','重开后原批依据改变')
                require(stock(win.catalogs['河北仓'])==20 and stock(win.catalogs['山东仓'])==700,'重开未采用两仓各自当前资料')
                checks.append('实际 EXE 重开后，原批河北库存 8,628、分配 360；新当前版本河北 20、山东 700')
            else:
                dialog=GenerationDialog(win,win,heads);dialog.setModal(True);dialog.show();wait(lambda:bool(dialog.catalogs))
                dialog.warehouse.setCurrentText('河北仓');dialog.order_mode.setCurrentIndex(1)
                QTest.mouseClick(dialog.preview_btn,Qt.MouseButton.LeftButton)
                old=dialog.plan['payload']['plans'][-1]['rows'][0]
                require((old['stock'],old['before'],old['quantity'])==('408','216','216'),'旧库存复现失败')
                comparison['before']={k:old[k] for k in ('stock','before','demand','quantity','after')}
                fresh=wc.import_catalog(out/'河北仓新资料.xlsx','河北仓')
                win.catalog_imported(fresh,expected_digest=repo.current_digest('河北仓'))
                require(stock(win.catalogs['河北仓'])==8628,'准备数据入口未更新当前仓资料')
                original_batches=batches.listing(path);before=len(messages)
                QTest.mouseClick(dialog.generate_btn,Qt.MouseButton.LeftButton)
                wait(lambda:dialog.worker is None and len(messages)>before)
                require(any('当前仓资料已刷新' in m['text'] for m in messages[before:]),'过期预览没有提示')
                require(batches.listing(path)==original_batches,'过期预览建立了新批次')
                checks.append('准备数据采用新版河北资料后，已打开的旧 408 库存预览被拦截，无新批次写入')
                dialog.warehouse.setCurrentText('山东仓');dialog.refresh_stock(str(out/'山东仓新资料.xlsx'))
                require(stock(win.catalogs['山东仓'])==700 and stock(repo.catalogs()['河北仓'])==8628,'另一仓刷新覆盖了河北仓')
                QTest.mouseClick(dialog.preview_btn,Qt.MouseButton.LeftButton)
                new=dialog.plan['payload']['plans'][-1]['rows'][0]
                require((new['stock'],new['before'],new['quantity'],new['after'])==('8628','8436','360','8076'),'新库存分配不足')
                comparison['after']={k:new[k] for k in ('stock','before','demand','quantity','after')}
                QTest.mouseClick(dialog.generate_btn,Qt.MouseButton.LeftButton)
                wait(lambda:dialog.worker is None and dialog.receipt is not None)
                require(len(dialog.receipt['generated'])==3 and not dialog.receipt['errors'],'实际生成失败')
                gid=dialog.receipt['generated'][-1];original=orders.get(path,gid)
                require(original['summary']['quantity']=='360','保存量与新预览不一致')
                checks.append('山东仓从分配窗口单独刷新后，河北仍用 8,628，前两店各 96，目标门店实际保存 360')
                repo.save(wc.import_catalog(out/'河北仓再刷新.xlsx','河北仓'))
                require(orders.get(path,gid)==original,'刷新覆盖已保存生成单')
                dialog.completed.item(2).setSelected(True)
                QTest.mouseClick(dialog.export_saved_btn,Qt.MouseButton.LeftButton);wait(lambda:dialog.export_worker is None)
                files=list(folder.glob('*.xlsx'));require(len(files)==1,'原批导出失败')
                book=openpyxl.load_workbook(files[0]);require(book.active['C2'].value==360,'原批导出未使用原数量');book.close()
                checks.append('河北当前资料再刷新至 20 后，已保存批次正常导出原 360 件')
                dialog.grab().save(str(out/'生成面板.png'))
            from version_info import diagnostic
            result={'passed':True,'frozen_exe':bool(getattr(sys,'frozen',False)),'checks':checks,'comparison':comparison,'messages':messages,'heartbeat_ticks':len(ticks),'max_tick_gap':max((b-a for a,b in zip(ticks,ticks[1:])),default=0),**diagnostic()}
        except Exception:result={'passed':False,'frozen_exe':bool(getattr(sys,'frozen',False)),'checks':checks,'messages':messages,'error':traceback.format_exc()}
        finally:
            timer.stop()
            if dialog and not dialog.worker and not dialog.export_worker:dialog.close();dialog.deleteLater()
        (out/('jst-basis-reopen.json' if reopening else 'jst-basis-report.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2),'utf-8');win._discard_close=True;app.exit(0 if result['passed'] else 1)
    QTimer.singleShot(100,execute)
