"""Isolated actual-window and stdio MCP acceptance for coverage analytics."""
import json, os, queue, subprocess, sys, threading, time, traceback, sqlite3, hashlib
from pathlib import Path
from contextlib import closing
from PySide6.QtCore import QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QMessageBox


def start_smoke(app, win, args):
    out=Path(args[0]).resolve()
    if not (out/'synthetic.json').is_file() or Path(os.environ.get('REPLENISHMENT_DATA_DIR','')).resolve()!=out/'settings':raise ValueError('Isolated synthetic directory required')
    def execute():
        checks=[];beats=[];client=None;timer=QTimer(win);timer.setInterval(25);timer.timeout.connect(lambda:beats.append(time.monotonic()));timer.start()
        def require(value,text):
            if not value:raise AssertionError(text)
            checks.append(text)
        def wait(predicate,seconds=90):
            end=time.monotonic()+seconds
            while not predicate() and time.monotonic()<end:QTest.qWait(10)
            if not predicate():raise AssertionError('Task timeout')
        class MCP:
            def __init__(self):
                from automation_connection import bridge_command
                command,argv=bridge_command();self.replies=queue.Queue();self.n=0;self.calls=[]
                self.log=(out/'mcp-process.log').open('w',encoding='utf-8')
                self.proc=subprocess.Popen([command,*argv,'--connection',str(win.automation.connection_file)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=self.log,text=True,encoding='utf-8',creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                def read():
                    for line in self.proc.stdout:self.replies.put(json.loads(line))
                threading.Thread(target=read,daemon=True).start()
                self.send('initialize',{'protocolVersion':'2025-11-25','capabilities':{},'clientInfo':{'name':'coverage-isolated-acceptance','version':'1'}})
            def send(self,method,params):
                self.n+=1;self.proc.stdin.write(json.dumps({'jsonrpc':'2.0','id':self.n,'method':method,'params':params})+'\n');self.proc.stdin.flush()
                wait(lambda:not self.replies.empty() or self.proc.poll() is not None)
                if self.proc.poll() is not None:raise AssertionError('MCP exited')
                response=self.replies.get_nowait()
                if response['id']!=self.n:raise AssertionError('MCP response id')
                return response
            def call(self,name,arguments=None):
                value=self.send('tools/call',{'name':name,'arguments':arguments or {}})['result']
                if value.get('isError'):raise AssertionError(value)
                data=value['structuredContent'];self.calls.append({'tool':name,'response':data});return data
            def close(self):
                self.proc.stdin.close();self.proc.wait(10);self.log.close()
        try:
            win.auto_check.stop();win.jst_generator.stopped=True;win.jst_generator.timer.stop();win.aoxiang_workflows.stopping=True;win.aoxiang_workflows.timer.stop()
            widget=win.analytics;win.show_section(3);win.tracking_tabs.setCurrentWidget(widget)
            spec=json.loads((out/'query.json').read_text('utf-8'));widget.start.setText(spec['from']);widget.end.setText(spec['to'])
            start=time.perf_counter();QTest.mouseClick(widget.query_btn,Qt.MouseButton.LeftButton);wait(lambda:widget.worker is None)
            query_seconds=time.perf_counter()-start
            require(widget.report is not None and len(widget.report['rows'])==4,'Native query returns both frozen rounds')
            widget.metric_choice.setCurrentIndex(3);app.processEvents()
            require('天数依据' in widget.coverage_note.text(),'Coverage view labels frozen basis and scope')
            require(widget.funnel.item(0,2).text().startswith('12.0天'),'Native signed coverage equals 24/2 = 12 days')
            widget.grab().save(str(out/'01-coverage.png'))
            widget.open_detail('store',0);widget.open_detail('warehouse',0);widget.open_detail('order',0)
            require('预测日均' in widget.models['detail'].headers,'Store warehouse order drill reaches Chinese coverage detail')
            modal=[]
            def dismiss():
                dlg=app.activeModalWidget()
                if isinstance(dlg,QMessageBox):modal.append(dlg.text());dlg.accept()
            QTimer.singleShot(150,dismiss);widget.coverage_explanation(widget.models['detail'].index(0,0))
            require(modal and '各段新增天数' in modal[0] and '预测日均' in modal[0],'Click explanation provides business formula and historical values')
            widget.grab().save(str(out/'02-detail.png'));widget.back();widget.tabs.setCurrentIndex(6)
            require(widget.models['rounds'].rowCount()==4 and widget.tables['rounds'].columnWidth(3)>=160
                    and any('待核' in row for row in widget.models['rounds'].rows),
                    'Round view retains both warehouses and old missing-forecast history with readable headers')
            widget.grab().save(str(out/'03-rounds.png'))
            import order_analytics_exports as exports
            receipts=[]
            for level in ('store','detail','coverage_rounds'):
                widget.start_task(exports.export,(win.work_repository.path,widget.report,out/(level+'.xlsx'),level),receipts.append);wait(lambda:widget.worker is None)
            require(len(receipts)==3,'Actual EXE produces summary detail and round exports')
            import openpyxl
            b=openpyxl.load_workbook(out/'store.xlsx')
            try:
                sheet=b['日期-店-SKU-仓'];column=list(next(sheet.values)).index('签发新增可售天数')+1
                require(sheet.cell(2,column).value==12 and sheet.cell(2,column).number_format=='0.0','Excel numeric days equal native displayed value')
                from funnel_dimension_export import NAMES
                require(b.sheetnames[:8]==NAMES,'Excel daily views retain five-stage days and identifiers')
            finally:b.close()
            win.automation.enable();client=MCP();ctx=client.call('workbench_context')
            task=client.call('query_order_funnel',{'context_token':ctx['context_token'],'operation_key':'coverage-exe-query','query':widget.report['query']})
            wait(lambda:win.automation.worker is None)
            status=client.call('task_status',{'task_id':task['task_id']})
            require(status['state']=='completed','Packaged stdio MCP query completes')
            result=client.call('order_funnel_results',{'task_id':task['task_id'],'level':'detail','limit':1})
            from decimal import Decimal
            require(Decimal(result['page']['items'][0]['coverage']['stage_days']['signed'])==12,'Packaged MCP same signed12 days')
            count=len(result['page']['items']);offset=result['page']['next_offset']
            while offset is not None:
                result=client.call('order_funnel_results',{'task_id':task['task_id'],'level':'detail','offset':offset,'limit':1});count+=len(result['page']['items']);offset=result['page']['next_offset']
            require(count==4,'MCP detail pagination covers all records without reordering')
            rounds=client.call('order_funnel_results',{'task_id':task['task_id'],'level':'rounds','limit':100})
            require(rounds['page']['total']==4,'MCP round view available')
            export_task=client.call('export_order_funnel',{'context_token':ctx['context_token'],'operation_key':'coverage-exe-export','task_id':task['task_id'],'snapshot_id':result['snapshot_id'],'directory':str(out),'level':'store'})
            wait(lambda:win.automation.worker is None);status=client.call('task_status',{'task_id':export_task['task_id']})
            require(status['state']=='completed','Packaged MCP exports same frozen report')
            ai_path=Path(status['receipt']['path']);books=[openpyxl.load_workbook(p) for p in (out/'store.xlsx',ai_path)]
            try:
                require(books[0].sheetnames==books[1].sheetnames,'Native and MCP export sheet order identical')
                for name in books[0].sheetnames:
                    a=list(books[0][name]);b=list(books[1][name])
                    if len(a)!=len(b):raise AssertionError('AI export row count')
                    for ra,rb in zip(a,b):
                        if [(c.value,c.data_type,c.number_format,c.style_id) for c in ra]!=[(c.value,c.data_type,c.number_format,c.style_id) for c in rb]:raise AssertionError('AI export differs '+name)
                checks.append('Native and packaged MCP all exported values types formats styles identical')
            finally:
                for book in books:book.close()
            (out/'mcp-calls.json').write_text(json.dumps([c for c in client.calls if c['tool']!='workbench_context'],ensure_ascii=False,indent=2),'utf-8')
            client.close();client=None
            QTest.qWait(100);win.automation.disable();QTest.qWait(100)
            widget.query();widget.cancel();wait(lambda:widget.worker is None);widget.query();wait(lambda:widget.worker is None)
            require(widget.report is not None,'Stop and fresh retry retain a valid result')
            widget.coverage_through.setCurrentIndex(4);widget.query();wait(lambda:widget.worker is None)
            require(widget.report['coverage']['overview']['orders']==1,'Receipt comparison uses one complete order, not unknown as zero')
            widget.metric_choice.setCurrentIndex(3);widget.tabs.setCurrentIndex(2);widget.grab().save(str(out/'04-receipt-cohort.png'))
            # Deliberate isolated stale-basis injection, restored before exiting.
            with closing(sqlite3.connect(win.work_repository.path)) as db,db:
                saved=db.execute('SELECT oid,revision,digest FROM normal_revisions').fetchall();db.execute("UPDATE normal_revisions SET digest='isolated-change'")
            widget.check_update();wait(lambda:widget.basis_worker is None)
            require(widget.stale and not widget.export_btn.isEnabled(),'Changed basis disables stale export')
            with closing(sqlite3.connect(win.work_repository.path)) as db,db:
                for oid,revision,digest in saved:db.execute('UPDATE normal_revisions SET digest=? WHERE oid=? AND revision=?',(digest,oid,revision))
            widget.query();wait(lambda:widget.worker is None);require(not widget.stale,'Refresh restores valid export after basis restored')
            from version_info import diagnostic
            report={'passed':True,'frozen_exe':bool(getattr(sys,'frozen',False)),**diagnostic(),'checks':checks,'query_seconds':query_seconds,'heartbeat_ticks':len(beats),'max_heartbeat_gap_seconds':max((b-a for a,b in zip(beats,beats[1:])),default=0),
                    'limits':'Synthetic isolated database, actual Qt interaction and actual stdio MCP bridge; not a real Codex/WorkBuddy language-response assessment. No user database or platform accessed.'}
        except Exception:report={'passed':False,'checks':checks,'error':traceback.format_exc(),'frozen_exe':bool(getattr(sys,'frozen',False))}
        finally:
            if client:client.close()
            timer.stop()
        (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
        win._discard_close=True;win.job_dirty=False;app.exit(0 if report['passed'] else 1)
    QTimer.singleShot(300,execute)
