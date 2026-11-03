"""Opt-in actual generation-panel acceptance; isolated prepared SQLite only."""
import copy,json,os,sys,time,traceback,sqlite3
from pathlib import Path
from contextlib import closing
from PySide6.QtCore import QTimer,Qt
from PySide6.QtWidgets import QMessageBox,QPushButton
from PySide6.QtTest import QTest

def start_smoke(app,win,args):
    out=Path(args[0]).resolve();checks=[];messages=[];ticks=[];dialog=None
    win.auto_check.stop();win.jst_generator.stopped=True;win.jst_generator.timer.stop();win.aoxiang_workflows.stopping=True;win.aoxiang_workflows.timer.stop()
    def tick():
        ticks.append(time.monotonic());box=app.activeModalWidget()
        if isinstance(box,QMessageBox):
            messages.append({'title':box.windowTitle(),'text':box.text()})
            if box.windowTitle()=='确认生成聚水潭订单':box.button(QMessageBox.StandardButton.Yes).click()
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
            import warehouse_catalog as wc,jst_orders as orders,jst_results as results,order_lifecycle as lc,work_repository as wr,export_settings,export_locations,openpyxl
            from jst_ui import GenerationDialog
            path=win.work_repository.path
            require(Path(os.environ.get('REPLENISHMENT_DATA_DIR','')).resolve()==out/'settings','必须使用隔离数据目录')
            folder=out/'exports';folder.mkdir(exist_ok=True);export_settings.save(export_locations.config_path(win),str(folder))
            head=next(h for h in lc.current_targets(path) if h['warehouse']=='河北仓' and h['store']=='T')
            repo=wc.Repository(path.parent);base=repo.catalogs()['河北仓'];base['items']['0001']['可用数']=100;base['revision']=wc.revision(base['items'])
            orders.commit_refresh(path,orders.prepare_refresh(path,'河北仓',catalog=base))
            def tags(value):
                c=copy.deepcopy(base);c['items']['0001']['商品标签']=value;c['revision']=wc.revision(c['items']);repo.save(c)
            tags('必卖,二类医疗,其他')
            dialog=GenerationDialog(win,win,[head]);dialog.setModal(True);dialog.show();wait(lambda:bool(dialog.catalogs));dialog.warehouse.setCurrentText('河北仓')
            QTest.mouseClick(dialog.preview_btn,Qt.MouseButton.LeftButton)
            require(dialog.plan['payload']['plans'][0]['rows'][0]['quantity']=='0','当前医疗标签未拦截')
            dialog.accept_issues.setChecked(True);QTest.mouseClick(dialog.generate_btn,Qt.MouseButton.LeftButton);wait(lambda:dialog.worker is None and dialog.receipt is not None)
            require(not dialog.receipt['generated'] and not dialog.receipt['errors'],'受限商品生成正数单')
            with closing(sqlite3.connect(path)) as db:require(not orders.reservations(db,'河北仓',set()),'受限商品占用库存')
            checks.append('实际预览/勾选/确认生成：旧库存100、当前医疗标签及待核验资质，执行0且无库存预留')
            tags('');QTest.mouseClick(dialog.preview_btn,Qt.MouseButton.LeftButton);QTest.mouseClick(dialog.generate_btn,Qt.MouseButton.LeftButton);wait(lambda:dialog.worker is None and dialog.receipt is not None)
            require(len(dialog.receipt['generated'])==1 and not dialog.receipt['errors'],'正常商品未生成')
            gid=dialog.receipt['generated'][0];require(orders.get(path,gid)['summary']['quantity']=='40','正常需求数量变化')
            dialog.completed.item(0).setSelected(True)
            button=next(b for b in dialog.findChildren(QPushButton) if b.text()=='导出所选已生成单')
            QTest.mouseClick(button,Qt.MouseButton.LeftButton);wait(lambda:dialog.export_worker is None)
            files=list(folder.glob('*.xlsx'));require(len(files)==1,'有效单导出失败');original=files[0].read_bytes()
            newer=copy.deepcopy(base);newer['items']['0001']['可用数']=20;newer['revision']=wc.revision(newer['items'])
            orders.commit_refresh(path,orders.prepare_refresh(path,'河北仓',catalog=newer))
            before=len(messages);QTest.mouseClick(button,Qt.MouseButton.LeftButton);wait(lambda:dialog.export_worker is None)
            require(any('更新' in m['text'] for m in messages[before:]),'过期导出没有提示');require(files[0].read_bytes()==original,'过期导出覆盖了原文件')
            data=results.read(path,out/'results-index.sqlite3',wr.today());require(not results.export_plan(data)['entries'],'结果页普通范围包含旧结果')
            history=results.export(path,results.export_plan(data,allow_old=True),folder);require('旧版本' in Path(history['files'][0]['path']).name,'旧版本未另名')
            b=openpyxl.load_workbook(history['files'][0]['path']);require(b.active['C2'].value==40,'旧结果数量改变');b.close()
            checks.append('实际生成面板有效单导出40件；库存更新后同按钮拒绝，原文件不变；结果页普通范围也排除；明确旧结果服务另名输出原40件')
            # Exercise the actual template slot with an isolated filename provider.
            template=out/'最低仓资料模板.xlsx';original_save=export_locations.save_file
            try:
                export_locations.save_file=lambda *a,**k:(str(template),'Excel (*.xlsx)');dialog.template()
            finally:export_locations.save_file=original_save
            b=openpyxl.load_workbook(template);s=b.worksheets[0];headers=[c.value for c in s[1]];require('商品标签' in headers,'模板缺少标签')
            s.append([newer['items']['0001'].get(k) for k in headers]);b.save(template);b.close()
            c=wc.import_catalog(template,'河北仓');require('商品标签' in c['items']['0001'],'空标签列被丢失')
            b=openpyxl.load_workbook(template);b.worksheets[0].delete_cols(headers.index('商品标签')+1);missing=out/'缺标签.xlsx';b.save(missing);b.close()
            before=len(messages);dialog.refresh_stock(str(missing));require(any('商品标签' in m['text'] for m in messages[before:]),'缺列导入没有提前提示')
            checks.append('实际模板入口（QA注入保存路径）包含标签列；空标签保留；实际导入缺列文件时提前提示')
            dialog.grab().save(str(out/'生成面板.png'))
            from version_info import diagnostic
            result={'passed':True,'frozen_exe':bool(getattr(sys,'frozen',False)),'checks':checks,'messages':messages,'heartbeat_ticks':len(ticks),'max_tick_gap':max((b-a for a,b in zip(ticks,ticks[1:])),default=0),**diagnostic()}
        except Exception:result={'passed':False,'frozen_exe':bool(getattr(sys,'frozen',False)),'checks':checks,'messages':messages,'error':traceback.format_exc()}
        finally:
            timer.stop()
            if dialog and not dialog.worker and not dialog.export_worker:dialog.close();dialog.deleteLater()
        (out/'jst-basis-report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),'utf-8');win._discard_close=True;app.exit(0 if result['passed'] else 1)
    QTimer.singleShot(100,execute)
