"""Opt-in actual-window acceptance. Requires a new isolated QA settings directory."""
import copy,json,os,sys,time,traceback
from pathlib import Path
from PySide6.QtCore import QTimer,QEventLoop,QUrl
from PySide6.QtWidgets import QApplication,QMessageBox,QDialog
import batch_service,warehouse_catalog,new_product_focus,work_repository as wr
import normal_orders,purchases,history_read,business_presenter as view,business_log_views
from version_info import diagnostic


def start_smoke(app,win,args):
    fixture,out=[Path(s).resolve() for s in args[:2]];checks=[];timings={};pictures=[];opened=[]
    def wait(predicate,timeout=30):
        end=time.monotonic()+timeout
        while not predicate() and time.monotonic()<end:
            loop=QEventLoop();QTimer.singleShot(10,loop.quit);loop.exec()
        if not predicate():raise AssertionError('UI operation did not complete')
    def shot(w,name):
        app.processEvents();name=name+'.png'
        if not w.grab().save(str(out/name)):raise AssertionError('Screenshot not saved')
        pictures.append(name)
    def require(condition,message):
        if not condition:raise AssertionError(message)
        checks.append(message)
    def execute():
        result={};beat=QTimer();beat.setInterval(20);ticks=[];beat.timeout.connect(lambda:ticks.append(time.perf_counter()));beat.start()
        try:
            if Path(os.environ.get('REPLENISHMENT_DATA_DIR','')).resolve()!=out/'settings':raise ValueError('Isolated settings required')
            if win.orders.headers():raise ValueError('QA must start without orders')
            win.jst_generator.timer.stop();win.jst_generator.stopped=True;win.aoxiang_workflows.timer.stop()
            catalogs={w:warehouse_catalog.import_catalog(fixture/(w+'.xlsx'),w) for w in warehouse_catalog.WAREHOUSES}
            data=batch_service.load([fixture/'sales.xlsx'],catalogs);win.loaded_ok(data)
            win.profile=json.loads((fixture/'must.json').read_text('utf-8'));win.new_focus=new_product_focus.empty(wr.today()[:7])
            codes=list(win.store_widgets)
            for c in codes:win.set_store_ui(c,{**win.store_ui(c),'mode':1,'primary':'河北仓','secondary':'山东仓'})
            win.start_calculation(codes);wait(lambda:win.worker is None,60)
            require(all(win.states[c]['state']=='已完成' for c in codes),'Frozen Windows spawn calculates two stores successfully')
            original=copy.deepcopy(win.results);win.current_store=codes[0];win.result=win.results[codes[0]];win.dirty=False
            win.set_rows(win.result['rows'],True);win.navigate(4);win.show_section(1);win.resize(1440,1000)
            require(win.table.isColumnHidden(32) and not win.table.isColumnHidden(9),'Default columns show result; technical columns remain available')
            before=win.visible_rows();win.column_choice.setCurrentText('全部字段')
            require(win.visible_rows()==before and not win.table.isColumnHidden(32),'Changing visible columns does not alter filtered records/order/export scope')
            win.column_choice.setCurrentText('主要结果');win.mode.setCurrentText('全部商品');win.warehouse_filter.setCurrentText('河北仓')
            win.result_facets.fields['priority'].setCurrentIndex(0) if 'priority' in win.result_facets.fields else None
            require(win.active_conditions.count()>=3,'All active filter conditions remain visible and removable')
            index=win.proxy.index(0,0);win.show_detail(index);selected=win.model.rows[win.proxy.mapToSource(index).row()]
            win.table.sortByColumn(1,win.table.horizontalHeader().sortIndicatorOrder())
            win.details.anchorClicked.emit(QUrl('parameters'));require('本商品实际参数' in win.details.toPlainText(),'Actual parameter detail opens after table sorting')
            win.details.anchorClicked.emit(QUrl('back'));require(str(selected.get('D','')) in win.details.toPlainText(),'Detail stays bound to originally selected SKU after sorting')
            shot(win,'01_工作商品结果与条件');win.reset_result_filters()
            repo=normal_orders.Repository(win.work_repository);repo.confirm(win.snapshot(),codes,None,{c:None for c in codes},headers_only=True)
            heads=purchases.targets(win.work_repository.path);head=heads[0];order=repo.get(head['day'],head['store'],head['revision']);order['warehouse']=head['warehouse'];head['name']=order['payload']['result']['store_name']
            saved=copy.deepcopy(order);d=next(x for x in order['payload']['result']['rows'] if x.get('AU')==head['warehouse'] and isinstance(x.get('AN'),(float,int)))
            from history_ui import AuditDialog,EventsDialog
            audit=AuditDialog(win,win,head,sku=d['E']);opened.append(audit);audit.resize(1380,870);audit.show();wait(lambda:audit.loaded_key is not None)
            for scope in ('决策总览','数量形成过程','当时参数','商品完整计算依据'):
                start=time.perf_counter()
                if audit.scope.currentText()==scope:audit.load()
                else:audit.scope.setCurrentText(scope)
                wait(lambda:audit.loaded_key==(scope,d['E']));timings[scope]=time.perf_counter()-start
                require(audit.model.rows==view.historical(repo,order,scope,d['E']),scope+' matches immutable snapshot projection')
                shot(audit,'02_'+scope)
            audit.search.setText('AN');require(audit.model.rowCount()>0,'Old field code search resolves Chinese business name')
            # Rapid requests must keep only the newest selection.
            audit.scope.setCurrentText('数量形成过程');audit.scope.setCurrentText('当时参数');audit.scope.setCurrentText('决策总览')
            wait(lambda:audit.loaded_key==('决策总览',d['E']))
            require(audit.model.rows==view.historical(repo,order,'决策总览',d['E']),'Rapid view changes never publish stale request results')
            target=out/'本商品决策依据.xlsx';received=[];errors=[]
            audit.reader.request({'kind':'form_export','head':head,'scope':'决策总览','sku':d['E'],'business_view':True,'path':str(target)},received.append,errors.append)
            wait(lambda:bool(received or errors));require(not errors and target.exists(),'Business form export completes through frozen read-only worker')
            import openpyxl
            book=openpyxl.load_workbook(target);values=list(book.active.values);book.close()
            expected=view.historical(repo,order,'决策总览',d['E'])
            normalize=lambda rows:[[None if x=='' else x for x in r] for r in rows]
            require(normalize(values)==normalize([view.HEADERS,*expected]),'Business Excel headers, values, types and row order match full scope even when UI search is active')
            audit.dispose();audit.hide()
            from parameter_ui import ParameterDialog
            import parameter_schema as ps
            config=wr.loads(wr.dumps(ps.standard()));p=win.work_repository.presets()[0]
            param=ParameterDialog(win,config,p,scope='所选1家门店');opened.append(param);param.show();app.processEvents()
            require(param.value()==config,'Opening parameter editor does not change effective configuration')
            shot(param,'03_实际值与适用范围');param.tabs.setCurrentWidget(param.param_table);shot(param,'04_百分比参数')
            row=list(ps.HELP).index('probability');spin=param.param_table.cellWidget(row,1);spin.lineEdit().setText('95');spin.interpretText()
            require(spin.value()==.95 and param.value()['parameters']['probability']==.95,'Typed 95 percent retains fractional 0.95 internal contract')
            param.fill(config);param.reject()
            from readable_ui import review_table,file_results,failure
            box=QMessageBox(win);box.setWindowTitle('核对签发变化');review_table(box,['商品','原量','新量'],[['商品甲',5,0],['商品乙',0,6]])
            cancel=box.addButton('取消',QMessageBox.ButtonRole.RejectRole);box.addButton('确认',QMessageBox.ButtonRole.AcceptRole);box.setDefaultButton(cancel)
            def cancel_review():shot(box,'05_可搜索差异确认');cancel.click()
            QTimer.singleShot(200,cancel_review);box.exec();require(box.clickedButton()==cancel,'Review cancel is default and leaves original snapshot unchanged')
            events=business_log_views.events(win.work_repository.path,head['oid'],head['warehouse']);logs=EventsDialog(win,events['records'],events['rows']);opened.append(logs);logs.show();logs.table.selectRow(0);logs.selection_changed();shot(logs,'06_操作日志业务表单');logs.technical.setChecked(True);shot(logs,'07_日志技术详情');logs.close()
            from jst_ui import Logs
            log=Logs(win,win,win.job_day,win.job_day);opened.append(log);log.show();wait(lambda:hasattr(log,'records'));log.table.selectRow(0);log.show_detail(log.table.currentIndex());shot(log,'08_批次日志');log.close()
            def close_file_dialog():
                current=QApplication.activeModalWidget()
                if current:shot(current,'09_导出文件列表');current.reject()
            QTimer.singleShot(200,close_file_dialog);file_results(win,[str(target)])
            def close_failure():
                current=QApplication.activeModalWidget()
                if current:shot(current,'10_受理未核实提示');current.accept()
            QTimer.singleShot(200,close_failure);failure(win,'翱象操作未完成','SUBMISSION_UNKNOWN: QA simulated only')
            current=repo.get(head['day'],head['store'],head['revision']);current['warehouse']=head['warehouse']
            require(current==saved and win.results==original,'Review, filtering, export and cancellation leave saved order and calculation results strictly unchanged')
            result={'passed':True,'frozen_exe':bool(getattr(sys,'frozen',False)),'checks':checks,'timings_seconds':timings,'heartbeat_ticks':len(ticks),'screenshots':pictures,**diagnostic()}
        except Exception:result={'passed':False,'checks':checks,'error':traceback.format_exc(),**diagnostic()}
        finally:
            beat.stop()
            from history_tasks import HistoryReader
            for r in win.findChildren(HistoryReader):r.shutdown()
            win.aoxiang_workflows.shutdown();win.stop_filtered_summary()
            (out/'readability-report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
            app.exit(0 if result.get('passed') else 1)
    QTimer.singleShot(200,execute)
