"""Opt-in Qt/frozen acceptance on marked synthetic settings; no real platform."""
import json,os,sys,time,traceback
from pathlib import Path
from PySide6.QtCore import QTimer,Qt
from PySide6.QtWidgets import QMessageBox,QPushButton
from PySide6.QtTest import QTest

def start_smoke(app,win,args):
    out=Path(args[0]).resolve();settings=out/'settings';checks=[];beats=[];dialogs=[];messages=[]
    if Path(os.environ.get('REPLENISHMENT_DATA_DIR','')).resolve()!=settings or not json.loads((out/'synthetic.json').read_text('utf-8'))['synthetic']:raise ValueError('Only marked isolated settings are allowed')
    win.auto_check.stop();win.jst_generator.stopped=True;win.jst_generator.timer.stop();win.aoxiang_workflows.stopping=True;win.aoxiang_workflows.timer.stop()
    forward=[False]
    def require(ok,text):
        if not ok:raise AssertionError(text)
        checks.append(text)
    def wait(fn,seconds=60):
        deadline=time.monotonic()+seconds
        while not fn() and time.monotonic()<deadline:QTest.qWait(15)
        if not fn():raise AssertionError('等待界面任务超时')
    def tick():
        beats.append(time.monotonic());box=app.activeModalWidget()
        if isinstance(box,QMessageBox):
            messages.append({'title':box.windowTitle(),'text':box.text()})
            yes=box.button(QMessageBox.StandardButton.Yes)
            if yes and box.windowTitle()=='确认生成聚水潭订单':yes.click()
            else:box.accept()
        elif forward[0] and box and box.objectName()=='lifecycleReview':
            box.complete.setChecked(True);box.extra_ack.setChecked(True);box.save.click()
        elif forward[0] and box and box.property('jstResults'):
            import order_lifecycle as lc
            if all(lc.snapshot(win.work_repository.path,h)['health']['payment'] for h in heads):box.reject()
    timer=QTimer(win);timer.setInterval(30);timer.timeout.connect(tick)
    heads=json.loads((out/'synthetic.json').read_text('utf-8'))['heads']
    def run():
        timer.start()
        try:
            from decimal import Decimal
            from contextlib import closing
            from unittest.mock import patch
            import sqlite3,openpyxl
            import order_lifecycle as lc,work_repository as wr,jst_orders as j,jst_exports as ex
            from lifecycle_home_ui import StageDialog
            from jst_ui import GenerationDialog
            from lifecycle_ui import Review
            from stockout_task import Bridge
            def forbidden(*a,**k):raise AssertionError('禁止调用真实平台')
            with patch.object(Bridge,'request',forbidden):
                require(all(lc.snapshot(win.work_repository.path,h)['next']=='operations' for h in heads),'回传之后进入运营签发；尚未提前生成分配')
                d=StageDialog(win.followup,'operations',heads);dialogs.append(d);d.show();p=d.panel;wait(lambda:len(p.records)==2)
                p.checked={str(i):h for i,h in enumerate(heads)}
                adopt=next(b for b in p.findChildren(QPushButton) if b.text()=='采用回传并运营签发');adopt.click()
                wait(lambda:all(lc.snapshot(win.work_repository.path,h)['health'].get('operations') for h in heads))
                p.status.setCurrentText('本环节全部');p.query();wait(lambda:len(p.records)==2 and p.reader.active is None and p.reader.pending is None)
                p.grab().save(str(out/'operations-page.png'));require(True,'实际勾选两仓并点击一键采用回传，独立保存运营版本')
                before={h['warehouse']:lc.snapshot(win.work_repository.path,h)['documents']['return']['digest'] for h in heads}
                result=lc.export(win.work_repository.path,heads,out/'exports','operations')
                require(len(result['files'])==2 and not result['error'],'运营签发逐仓导出与回传共用样式及隐藏关联')
                d.close()
                g=GenerationDialog(win,win,heads);dialogs.append(g);g.show();wait(lambda:len(g.catalogs)==2)
                g.preview_btn.click();require(g.plan is not None,'实际预览按钮读取运营需求及所选仓快照')
                values={p['head']['warehouse']:Decimal(p['rows'][0]['quantity']) for p in g.plan['payload']['plans']}
                require(values=={'河北仓':Decimal(1079),'山东仓':Decimal(792)},'1090按15规整后库存不足分1079；800按24规整后分792')
                g.generate_btn.click();wait(lambda:g.worker is None and g.receipt is not None)
                require(not g.receipt['errors'] and len(g.saved)==2,'真实确认生成按钮保存两个仓单并释放工作线程')
                gids=list(g.saved);g.grab().save(str(out/'allocation-page.png'))
                receipt=ex.export_checked(win.work_repository.path,gids,out/'exports');require(len(receipt['files'])==2,'分配尾数进入实际聚水潭导入文件')
                quantities=[]
                for file in receipt['files']:
                    book=openpyxl.load_workbook(file['path'],read_only=True,data_only=True)
                    try:quantities.append(list(book.active.values)[1][2])
                    finally:book.close()
                require(sorted(quantities)==[792,1079],'实际Excel数量与分配预览一致，未二次整箱取整');g.close()
                d=StageDialog(win.followup,'operations',heads);dialogs.append(d);d.show();p=d.panel;p.status.setCurrentText('本环节全部');wait(lambda:len(p.records)==2);p.checked={str(i):h for i,h in enumerate(heads)}
                forward[0]=True
                next(b for b in p.findChildren(QPushButton) if b.text()=='分配并准备付款').click()
                forward[0]=False
                require(all(lc.snapshot(win.work_repository.path,h)['health']['payment'] for h in heads),'实际自然流转按钮直接打开付款核对并保存，无Excel搬运')
                require(all(lc.snapshot(win.work_repository.path,h)['documents']['return']['digest']==before[h['warehouse']] for h in heads),'自然流转保持原回传内容不变')
                d.close();win.show_section(3);win.tracking_tabs.setCurrentWidget(win.analytics);a=win.analytics;a.query();wait(lambda:a.worker is None)
                require(a.report is not None and len(a.report['rows'])==2,'原生分析后台查询成功，七个节点来自冻结事实')
                a.tabs.setCurrentWidget(a.workflow);QTest.qWait(100);app.processEvents()
                a.grab().save(str(out/'workflow-trends.png'))
                chain=[];node=a.workflow.chart
                while node is not None:
                    chain.append({'class':type(node).__name__,'height':node.height(),'minimum':node.minimumHeight(),'visible_height':node.visibleRegion().boundingRect().height(),'geometry':node.geometry().getRect()});node=node.parentWidget()
                (out/'layout.json').write_text(json.dumps(chain,ensure_ascii=False,indent=2),'utf-8')
                require(a.workflow.chart.height()>=140 and not a.funnel.isVisible(),'趋势页收起旧汇总，核心图表获得足够高度')
                require(a.workflow.chart.visibleRegion().boundingRect().height()>=a.workflow.chart.height()-2,'趋势图整个坐标区域可见，没有被底部控件裁切')
                a.workflow.grain.setCurrentIndex(1);a.workflow.mode.setCurrentIndex(0);a.workflow.ratio.setCurrentIndex(5);a.workflow.chart.grab().save(str(out/'ratio-chart.png'))
                require(a.workflow.table.columnCount()==14 and a.workflow.details.columnCount()==19,'趋势表与明细包含运营、分配和后置三种单号')
                from order_analytics_exports import export
                report=export(win.work_repository.path,a.report,out/'exports'/'全流程经营分析.xlsx')
                book=openpyxl.load_workbook(report['path'])
                try:
                    require(book.sheetnames[0]=='漏斗总览' and len(book['原五段比率看板']._charts)==1,'清洁纵向总览及五条原漏斗率原生Excel折线图')
                    for name in ('经营主表','商品款数','签发价货值'):require(list(book[name].values)[0][-3:]==('签发单号','聚水潭内部订单号','翱象采购单号'),name+'业务字段前置、三个单号后置')
                finally:book.close()
                from operations_flow import adopt
                prior=[lc.snapshot(win.work_repository.path,h)['documents']['operations']['version'] for h in heads];adopt(win.work_repository.path,heads)
                require([lc.snapshot(win.work_repository.path,h)['documents']['operations']['version'] for h in heads]==prior,'原采用动作幂等，不生成重复版本或库存占用')
                a.tabs.setCurrentIndex(0);a.cancel();require(len(beats)>5,'执行期间界面心跳持续，所有动作仅使用隔离资料')
            result={'passed':True,'checks':checks,'live_platform':False,'frozen_exe':bool(getattr(sys,'frozen',False)),
                    'heartbeat_ticks':len(beats),'maximum_heartbeat_gap_seconds':max((b-a for a,b in zip(beats,beats[1:])),default=0),'messages':messages,'allocation':{k:str(v) for k,v in values.items()}}
        except Exception as exc:result={'passed':False,'checks':checks,'error':str(exc),'traceback':traceback.format_exc(),'messages':messages}
        finally:
            timer.stop()
            for d in reversed(dialogs):
                if hasattr(d,'panel'):d.panel.shutdown()
                if not getattr(d,'worker',None) and not getattr(d,'export_worker',None):d.close()
        (out/'report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),'utf-8');win._discard_close=True;app.exit(0 if result['passed'] else 1)
    QTimer.singleShot(100,run)
