"""Actual GUI and packaged stdio MCP acceptance, restricted to marked synthetic data."""
import json,os,queue,subprocess,sys,threading,time,traceback,sqlite3
from contextlib import closing
from pathlib import Path
from decimal import Decimal
from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QMessageBox


def start_smoke(app,win,args):
    out=Path(args[0]).resolve()
    if not (out/'synthetic.json').is_file() or Path(os.environ.get('REPLENISHMENT_DATA_DIR','')).resolve()!=out/'settings':raise ValueError('Synthetic isolation required')
    def execute():
        checks=[];beats=[];client=None;timer=QTimer(win);timer.setInterval(25);timer.timeout.connect(lambda:beats.append(time.monotonic()));timer.start()
        def require(v,text):
            if not v:raise AssertionError(text)
            checks.append(text)
        def wait(test,seconds=90):
            end=time.monotonic()+seconds
            while not test() and time.monotonic()<end:QTest.qWait(10)
            if not test():raise AssertionError('Timeout')
        class MCP:
            def __init__(self):
                from automation_connection import bridge_command
                command,argv=bridge_command();self.replies=queue.Queue();self.n=0;self.calls=[];self.log=(out/'mcp.log').open('w',encoding='utf-8')
                self.proc=subprocess.Popen([command,*argv,'--connection',str(win.automation.connection_file)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=self.log,text=True,encoding='utf-8',creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                def read():
                    for line in self.proc.stdout:self.replies.put(json.loads(line))
                threading.Thread(target=read,daemon=True).start();self.send('initialize',{'protocolVersion':'2025-11-25','capabilities':{},'clientInfo':{'name':'inventory-isolated','version':'1'}})
            def send(self,method,params):
                self.n+=1;self.proc.stdin.write(json.dumps({'jsonrpc':'2.0','id':self.n,'method':method,'params':params})+'\n');self.proc.stdin.flush();wait(lambda:not self.replies.empty() or self.proc.poll() is not None)
                if self.proc.poll() is not None:raise AssertionError('MCP exited')
                result=self.replies.get_nowait()
                if result['id']!=self.n:raise AssertionError('MCP id mismatch')
                return result
            def call(self,name,args=None):
                response=self.send('tools/call',{'name':name,'arguments':args or {}})['result']
                if response.get('isError'):raise AssertionError(response)
                self.calls.append({'tool':name,'response':response['structuredContent']});return response['structuredContent']
            def close(self):
                self.proc.stdin.close();wait(lambda:self.proc.poll() is not None,15);self.log.close();(out/'mcp-evidence.json').write_text(json.dumps(self.calls,ensure_ascii=False,indent=2),'utf-8')
        try:
            w=win.inventory;win.resize(1600,1000);win.show_section(3);win.tracking_tabs.setCurrentIndex(2)
            spec=json.loads((out/'query.json').read_text('utf-8'));w.start.setText(spec['from']);w.end.setText(spec['to']);start=time.monotonic();w.query_btn.click();wait(lambda:w.worker is None)
            require(w.report is not None,'Actual window background query completes');seconds=time.monotonic()-start
            require(len(w.report['rounds'])==4 and w.report['overview']['total']['value']==222,'Two stores two rounds, latest day not accumulated')
            require('地采' in w.cards.text() and '未证明全店' in w.scope.text(),'Business scope and local valuation visible')
            w.grab().save(str(out/'01-overview.png'));w.open_round(w.model.index(3,0));wait(lambda:w.worker is None)
            require(len(w.details)==4 and w.dmodel.rowCount()==4,'On-demand store round detail')
            w.filters['status'].setCurrentText('禁用');require(w.dmodel.rowCount()==1,'Disabled inventory retained and filterable');w.clear_filters()
            shown=[]
            def dismiss():
                dlg=app.activeModalWidget()
                if isinstance(dlg,QMessageBox):shown.append(dlg.text());dlg.accept()
            QTimer.singleShot(100,dismiss);w.explain(w.dmodel.index(0,0))
            require(shown and '数量变化影响' in shown[0],'Chinese formula and historical quantity/price explanation');w.grab().save(str(out/'02-detail.png'))
            from inventory_structure_exports import export
            receipts=[];report=w.report
            w.start_task(export,(win.work_repository.path,report,out/'native.xlsx',True),receipts.append);wait(lambda:w.worker is None);require(len(receipts)==1,'Actual EXE exports five-sheet detailed workbook')
            win.automation.enable();client=MCP();ctx=client.call('workbench_context')
            names=[t['name'] for t in client.send('tools/list',{})['result']['tools']];require('query_inventory_structure' in names,'Packaged bridge discovers inventory capabilities')
            args={'context_token':ctx['context_token'],'operation_key':'inventory-exe','query':report['query']}
            task=client.call('query_inventory_structure',args);wait(lambda:win.automation.worker is None);status=client.call('task_status',{'task_id':task['task_id']})
            require(status['state']=='completed','Packaged stdio MCP query completes')
            items=[];offset=0
            while offset is not None:
                result=client.call('inventory_structure_results',{'task_id':task['task_id'],'limit':1,'offset':offset});items+=result['page']['items'];offset=result['page']['next_offset']
            require(len(items)==4 and Decimal(result['overview']['total']['value'])==report['overview']['total']['value'],'MCP pagination and native totals agree')
            exported=client.call('export_inventory_structure',{'task_id':task['task_id'],'snapshot_id':result['snapshot_id'],'context_token':ctx['context_token'],'operation_key':'inventory-exe-export','directory':str(out),'include_details':True});wait(lambda:win.automation.worker is None)
            done=client.call('task_status',{'task_id':exported['task_id']});require(done['state']=='completed','Packaged MCP Excel export completes')
            import openpyxl
            books=[openpyxl.load_workbook(p) for p in (out/'native.xlsx',done['receipt']['path'])]
            try:
                require(books[0].sheetnames==books[1].sheetnames,'Native and MCP sheet order equal')
                compared=0
                for name in books[0].sheetnames:
                    aa,bb=list(books[0][name]),list(books[1][name]);require(len(aa)==len(bb),'Equal rows '+name)
                    for ra,rb in zip(aa,bb):
                        if name=='口径与待核' and ra[0].value=='查询时间':continue
                        if [(c.value,c.data_type,c.number_format,c.style_id) for c in ra]!=[(c.value,c.data_type,c.number_format,c.style_id) for c in rb]:raise AssertionError('Excel mismatch '+name)
                        compared+=len(ra)
            finally:
                for b in books:b.close()
            require(compared>100,'Every native/MCP workbook cell compared excluding exact query timestamp')
            w.query();w.cancel();wait(lambda:w.worker is None);w.query();wait(lambda:w.worker is None);require(w.report is not None,'Cancel followed by retry succeeds')
            for index in (1,2,0):
                w.view.setCurrentIndex(index);w.query();wait(lambda:w.worker is None);require(w.report['query']['view']==w.view.currentData(),'View changes only after successful query '+w.view.currentData())
            # Mutate only this marked synthetic database to test stale cache and deletion guards.
            with closing(sqlite3.connect(win.work_repository.path)) as db,db:db.execute("DELETE FROM normal_orders WHERE store='S000'")
            w.check_update();wait(lambda:w.worker is None);require(w.report is None,'Deleted order invalidates and clears visible cache')
            stale=client.send('tools/call',{'name':'inventory_structure_results','arguments':{'task_id':task['task_id']}})['result'];require(stale.get('isError'),'MCP refuses stale deleted snapshot')
            w.query();wait(lambda:w.worker is None);require(w.report['overview']['stores']==1,'Unselected store remains intact after deletion')
            w.query();require(w.prepare_close() is False,'Window close requests safe cancellation');wait(lambda:w.worker is None)
            from version_info import diagnostic
            result={'passed':True,'frozen_exe':bool(getattr(sys,'frozen',False)),**diagnostic(),'checks':checks,'query_seconds':seconds,'compared_cells':compared,'heartbeat_ticks':len(beats),'max_heartbeat_gap_seconds':max((b-a for a,b in zip(beats,beats[1:])),default=0),'limits':'Marked synthetic database only; actual Qt and packaged stdio bridge, not natural-language Codex/WorkBuddy assessment. No user data or platforms accessed.'}
        except Exception:result={'passed':False,'checks':checks,'error':traceback.format_exc(),'frozen_exe':bool(getattr(sys,'frozen',False))}
        finally:
            if client:client.close()
            timer.stop()
        (out/'report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),'utf-8');win._discard_close=True;win.job_dirty=False;app.exit(0 if result['passed'] else 1)
    QTimer.singleShot(300,execute)
