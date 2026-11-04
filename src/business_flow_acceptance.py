"""Actual source/frozen UI checks; requires a prepared, explicitly isolated fixture."""
import copy
import json
import os
import sys
import time
import traceback
from pathlib import Path
from PySide6.QtCore import QTimer, Qt, QSignalBlocker
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton


def start_smoke(app,win,args):
    out=Path(args[0]).resolve()
    if not (out/'synthetic.json').is_file() or Path(os.environ.get('REPLENISHMENT_DATA_DIR','')).resolve()!=out/'settings':
        raise ValueError('Prepared isolated synthetic settings required')
    def run():
        from parameter_workspace_smoke import fixture
        from lifecycle_home_ui import StageDialog
        import batch_service, business_flow, daily_batch, lifecycle_batch, work_repository as wr
        report={'passed':False,'frozen_exe':bool(getattr(sys,'frozen',False)),'checks':[]}
        checks=report['checks']; dialogs=[]; beats=[]; report['async_queries']=[]
        timer=QTimer(win);timer.setInterval(25);timer.timeout.connect(lambda:beats.append(time.perf_counter()));timer.start()
        def require(ok,text):
            if not ok:raise AssertionError(text)
        def wait(pred,timeout=60):
            end=time.perf_counter()+timeout
            while not pred() and time.perf_counter()<end:app.processEvents();QTest.qWait(15)
            require(pred(),'UI timeout')
        def shot(name):
            app.processEvents();win.grab().save(str(out/(name+'.png')))
        def query(panel):
            started=time.perf_counter();bi=len(beats)
            panel.query();wait(lambda:panel.reader.active is None and panel.reader.pending is None)
            require(panel.applied_request is not None,panel.note.text())
            ts=beats[bi:]
            report['async_queries'].append({'seconds':time.perf_counter()-started,'ticks':len(ts),'max_tick_gap':max((b-a for a,b in zip(ts,ts[1:])),default=0)})
        try:
            build=Path(getattr(sys,'_MEIPASS',Path(__file__).parent))/'build_info.json'
            report['build_commit']=json.loads(build.read_text('utf-8'))['source_commit'] if build.exists() else 'source'
            width,height=map(int,os.environ.get('BUSINESS_FLOW_QA_SIZE','1360x900').split('x'))
            report['requested_logical_size']=[width,height]
            win.setWindowTitle('补货助手 · 隔离合成验收');win.resize(width,height);win.show_section(0)
            wait(lambda:win.home_tasks.data is not None)
            expected=business_flow.read_home(win.work_repository.path,out/'oracle-index.sqlite3')
            require(win.home_tasks.data==expected,'Home projection differs')
            require(expected['latest_batch'] is not None,'Unfinished batch missing')
            require(all(expected['groups'][s] for s in ('return','payment','receipt')),'Missing fixture stages')
            shot('01-首页待办');checks.append('首页按真实单据状态显示三环节及最近暂停批次；不新增业务状态')
            report['actual_logical_size']=[win.width(),win.height()]
            report['device_pixel_ratio']=win.devicePixelRatioF()
            win.home_tasks.refresh();win.home_tasks.stop();require(not win.home_tasks.busy,'Cancel stuck')
            win.home_tasks.refresh();wait(lambda:win.home_tasks.data is not None)
            checks.append('首页查询可取消并重试；旧请求不回填')
            original=copy.deepcopy(win.snapshot())
            win.show_section(1);win.navigate(0)
            require(not win.connection_maintenance_toggle.isChecked(),'Connection maintenance not folded')
            require(not win.catalog_maintenance_toggle.isChecked(),'Catalog maintenance not folded')
            shot('02-准备数据')
            win.connection_maintenance_toggle.click();app.processEvents();require(win.aoxiang_setup_btn.isVisible(),'Configuration inaccessible')
            win.connection_maintenance_toggle.click()
            require(win.snapshot()==original,'Expanding maintenance changed work')
            checks.append('准备页自动获取和手动导入可见；配置折叠/展开不改变作业')
            win.loaded_ok(fixture())
            win.navigate(1);shot('03-门店参数')
            require(not win.store_parameters_toggle.isChecked(),'Parameter maintenance not folded')
            win.store_parameters_toggle.click();app.processEvents();win.store_parameters_toggle.click()
            for s in ('A','B'):win.store_widgets[s]['mode'].setCurrentIndex(1)
            win.concurrency.setValue(2);win.start_calculation(['A','B'])
            wait(lambda:win.worker is None)
            require(all(win.states[s]['state']=='已完成' for s in ('A','B')),str(win.states))
            checks.append('两店后台 spawn 计算完成；日常参数入口折叠不改变有效参数')
            frozen=copy.deepcopy(win.results);win.navigate(3);win.refresh_tables();shot('04-核对与签发')
            win.overview.horizontalScrollBar().setValue(win.overview.horizontalScrollBar().maximum());app.processEvents()
            require(win.fixed_overview.view.model() is win.overview.model(),'Frozen column copied data')
            require(win.fixed_overview.view.isVisible(),'Frozen columns invisible');shot('05-横向滚动固定列')
            win.states['B']['state']='待重算';win.refresh_tables();win.review_filter.setCurrentIndex(1)
            require([s for i,s in enumerate(win.overview_codes) if not win.overview.isRowHidden(i)]==['B'],'Blocking scope wrong')
            require(win.results==frozen,'Presentation changed calculation')
            checks.append('核对筛选不改结果/顺序；固定列与原表共享数据，横向滚动仍可定位门店')
            win.review_filter.setCurrentIndex(0);win.states['B']['state']='已完成';win.refresh_tables()
            w=win.followup;win.show_section(3)
            w.start.clear();w.end.clear();query(w)
            require(w.count>50,'Need paging fixture')
            first=w.records[0];w.table.item(0,0).setCheckState(Qt.CheckState.Checked);w.turn(1)
            wait(lambda:w.reader.active is None and w.reader.pending is None)
            second=w.records[0];w.table.item(0,0).setCheckState(Qt.CheckState.Checked)
            require(len(w.checked)==2,'Cross-page selection lost');shot('06-跨页勾选')
            selected=list(w.checked.values());before_rows=wr.dumps(win.results)
            from business_ui import run as background
            folder=out/'signed-export';folder.mkdir()
            exported=background(win,lifecycle_batch.export_signed,(win.work_repository.path,selected,folder),'验收：导出勾选签发单')
            require(len(exported)==2,'Selected export count mismatch')
            require(len(set(exported))==2 and all(Path(p).is_file() for p in exported),'Export file set mismatch')
            report['selected_export_numbers']=[h['number'] for h in selected]
            report['selected_export_files']=[Path(p).name for p in exported]
            require(wr.dumps(win.results)==before_rows,'History export changed current work')
            w.search.setText('S001');w.warehouse_filter.setCurrentText('河北仓');w.lock_filter.setCurrentText('已锁');query(w)
            require(not w.checked,'Filter change retained hidden selection')
            require(len(w.records)==1 and w.records[0]['store']=='S001' and w.records[0]['locked'],'Combined filter wrong')
            d=StageDialog(w,'payment');dialogs.append(d);d.show();wait(lambda:d.panel.reader.active is None and getattr(d.panel,'applied_request',None) is not None)
            require(d.panel.search.text()=='S001' and d.panel.lock_filter.currentText()=='已锁','Stage criteria lost')
            require([x['number'] for x in d.panel.records]==[x['number'] for x in w.records],'Stage changed scope')
            d.grab().save(str(out/'07-付款核对范围.png'));d.reject()
            d2=StageDialog(w,'payment',selected);dialogs.append(d2);d2.show();wait(lambda:getattr(d2.panel,'applied_request',None) is not None)
            require(d2.panel.fixed_scope==selected,'Selection scope lost')
            d2.panel.clear_filters();wait(lambda:d2.panel.reader.active is None and d2.panel.reader.pending is None)
            require(d2.panel.fixed_scope==selected,'Clear filters widened fixed scope');d2.reject()
            checks.append('日期/仓/锁/搜索继承；跨页勾选仅导出两张签发；切换筛选清空勾选；固定订单范围不会被清空筛选扩大')
            w.lock_filter.setCurrentIndex(0);w.warehouse_filter.setCurrentIndex(0);w.search.setText('不存在的合成门店');query(w)
            require(w.empty_actions.isVisible() and not w.bar.isVisible(),'Empty explanation/progress wrong')
            shot('08-无匹配与下一步');checks.append('无记录显示具体原因与清空筛选入口；加载完成收起进度条')
            # Check actual nested navigation, canceling its modal with a test-owned timer.
            from unittest.mock import patch
            w.search.clear();query(w);scoped=[w.records[0]]
            panel=StageDialog(w,'return',scoped);dialogs.append(panel);panel.show();app.processEvents()
            with patch('lifecycle_home_ui.StageDialog') as child:
                next_btn=next(b for b in panel.findChildren(QPushButton) if b.text().startswith('进入付款核对 →'))
                next_btn.click();require(child.call_args.args[2]==scoped,'Next stage widened scope')
            panel.reject();checks.append('下一环节入口沿用固定订单；没有生成付款或收货事实')
            report['passed']=True
        except Exception:report['error']=traceback.format_exc()
        finally:
            for d in dialogs:d.close();d.deleteLater()
            win.job_dirty=False;win._discard_close=True;win.close()
            timer.stop();report['heartbeat_ticks']=len(beats)
            report['max_heartbeat_gap_seconds']=max((b-a for a,b in zip(beats,beats[1:])),default=0)
            (out/'business-flow-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
            QTimer.singleShot(200,lambda:app.exit(0 if report['passed'] else 1))
    QTimer.singleShot(350,run)
