"""Actual Qt + stdio MCP + Windows spawn smoke; isolated synthetic workspace only."""
import copy
import json
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest


def start_smoke(app, win, args):
    out = Path(args[0]).resolve()
    if not (out / 'synthetic.json').is_file() or Path(os.environ.get('REPLENISHMENT_DATA_DIR', '')).resolve() != out / 'settings':
        raise ValueError('Explicit isolated synthetic directory required')
    def run():
        from parameter_workspace_smoke import fixture
        from automation_ui import bridge_command, connection_configs
        import batch_service
        report = {'passed': False, 'frozen_exe': bool(getattr(sys, 'frozen', False)), 'checks': []}; clients = []
        def require(value, message):
            if not value: raise AssertionError(message)
        def wait(predicate, timeout=30):
            end = time.monotonic() + timeout
            while not predicate() and time.monotonic() < end: app.processEvents(); QTest.qWait(10)
            require(predicate(), 'Timeout')
        class MCP:
            def __init__(self):
                command, arg = bridge_command(); self.n = 0; self.responses = queue.Queue()
                self.log = (out / ('bridge-' + str(len(clients)) + '.log')).open('w', encoding='utf-8')
                self.process = subprocess.Popen([command, *arg, '--connection', str(win.automation.connection_file)],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log, text=True, encoding='utf-8', creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                clients.append(self)
                def read():
                    for line in self.process.stdout: self.responses.put(json.loads(line))
                threading.Thread(target=read, daemon=True).start()
                self.send('initialize', {'protocolVersion': '2025-11-25', 'capabilities': {}, 'clientInfo': {'name': 'isolated-acceptance', 'version': '1'}})
            def send(self, method, params):
                self.n += 1
                self.process.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': self.n, 'method': method, 'params': params}) + '\n'); self.process.stdin.flush()
                wait(lambda: not self.responses.empty() or self.process.poll() is not None)
                require(self.process.poll() is None, 'Bridge exited'); value = self.responses.get_nowait(); require(value['id'] == self.n, 'Request id mismatch')
                return value
            def call(self, name, args=None, error=False):
                reply = self.send('tools/call', {'name': name, 'arguments': args or {}})['result']
                require(bool(reply.get('isError')) == error, str(reply))
                return reply['structuredContent']
            def close(self):
                self.process.stdin.close(); self.process.wait(10); self.log.close()
        try:
            win.loaded_ok(fixture())
            for code in ('A', 'B'): win.store_widgets[code]['mode'].setCurrentIndex(1)
            win.automation.enable(); m = MCP()
            require(len(m.send('tools/list', {})['result']['tools']) == 14, 'Missing tools')
            # Reproduce commit contention in the actual Qt process (including EXE).
            # Only task metadata is retried; the business callback must run once.
            import sqlite3
            locked_task,_=win.automation.journal.create('synthetic-lock',{},'prepare'); callbacks=[]
            lock=sqlite3.connect(win.work_repository.path)
            try:
                lock.execute('BEGIN');lock.execute('SELECT * FROM automation_tasks').fetchall()
                def locked_finish(value):
                    callbacks.append(value)
                    win.automation.journal.update(locked_task['task_id'],state='prepared')
                win.automation.start_job(locked_task,lambda _:42,locked_finish)
                wait(lambda:win.automation.worker is None)
                blocked=m.call('task_status',{'task_id':locked_task['task_id']})
                require(blocked['state']=='failed' and blocked['status_persistence_pending'],'Lock incorrectly reported as success')
            finally:lock.rollback();lock.close()
            wait(lambda:not win.automation.pending_failures)
            recovered=m.call('task_status',{'task_id':locked_task['task_id']})
            require(recovered['state']=='failed' and not recovered['status_persistence_pending'] and callbacks==[42], 'Failed callback replayed or status not recovered')
            require(len(recovered['events'])==1,'Rolled-back event duplicated')
            report['checks'].append('Actual SQLite commit lock: queryable failed task, metadata recovery after release, one event, no callback replay; fresh task continues')
            original = copy.deepcopy(win.snapshot()); context = m.call('workbench_context')
            p = m.call('prepare_calculation', {'context_token': context['context_token'], 'operation_key': 'smoke-prepare', 'stores': ['B', 'A'], 'mode': 'trial'})
            wait(lambda: win.automation.worker is None)
            p = m.call('task_status', {'task_id': p['task_id']}); require(p['state'] == 'prepared', str(p))
            parameters = m.call('effective_parameters', {'stores':['B','A'],'scope':'preview','preview_id':p['task_id']})
            require([s['store'] for s in parameters['stores']] == ['B','A'], 'Preview parameters reordered')
            require(parameters['stores'][0]['parameter_signature'] == win.automation.prepared[p['task_id']]['jobs']['B']['parameter_signature'], 'Preview signature mismatch')
            expected = {s: batch_service.calculate_store(win.loaded, s, win.selected_settings(s), win.profile) for s in ('B', 'A')}
            task = m.call('run_calculation', {'preview_id': p['task_id']})
            duplicate = m.call('run_calculation', {'preview_id': p['task_id']}); require(task['task_id'] == duplicate['task_id'], 'Duplicate launched task')
            wait(lambda: win.automation.worker is None, 60)
            status = m.call('task_status', {'task_id': task['task_id']}); require(status['state'] == 'completed', str(status))
            result = win.automation.require_result(task['task_id'])
            for s in ('B', 'A'):
                actual = result['results'][s]
                require(actual['rows'] == expected[s]['rows'], 'Business rows changed ' + s)
                require(actual['summary'] == expected[s]['summary'], 'Summary changed ' + s)
            require(win.snapshot() == original, 'Trial changed current workbench')
            report['checks'].append('Actual stdio initialize/list/call, ordered two-store spawn calculation, duplicate request, trial isolation and exact rows/summary equality')
            m2 = MCP(); query = m2.call('calculation_results', {'task_id': task['task_id']})
            require([x['store'] for x in query['stores']] == ['B', 'A'], 'Store order changed')
            m.close(); clients.remove(m)
            require(m2.call('explain_decision', {'task_id': task['task_id'], 'store': 'A', 'sku': '1001'})['calculation'], 'Missing explanation')
            report['checks'].append('Second client shares task and result; first client disconnection does not cancel; business explanation')
            before_context = m2.call('workbench_context')['context_token']
            win.store_widgets['A']['mode'].setCurrentIndex(2); win.store_widgets['A']['amount'].setText('10')
            rejected = m2.call('adopt_trial', {'task_id': task['task_id'], 'context_token': before_context}, error=True)
            require(rejected['code'] == 'STALE_CONTEXT', str(rejected)); require(not win.results, 'Stale result adopted')
            report['checks'].append('Manual parameter change rejects stale result without overwrite')
            def prepare(key, **kw):
                args = {'context_token': m2.call('workbench_context')['context_token'], 'operation_key': key, 'stores': ['B','A'], 'mode': 'trial', **kw}
                value = m2.call('prepare_calculation', args)
                require(m2.call('prepare_calculation', args)['task_id'] == value['task_id'], 'Prepare duplicate changed identity')
                wait(lambda: win.automation.worker is None)
                require(m2.call('task_status', {'task_id':value['task_id']})['state']=='prepared', 'Prepare failed')
                return value
            settings_before=copy.deepcopy(win.work_repository.stores())
            win.store_widgets['A']['mode'].setCurrentIndex(1)
            p2=prepare('custom',parameter_changes=[{'store':'A','path':['warehouses','山东仓',0],'value':10}])
            t2=m2.call('run_calculation',{'preview_id':p2['task_id']});wait(lambda:win.automation.worker is None,60)
            prepared=win.automation.prepared[p2['task_id']]
            oracle=batch_service.calculate_store(win.loaded,'A',prepared['jobs']['A'],win.profile)
            require(win.automation.require_result(t2['task_id'])['results']['A']['rows']==oracle['rows'],'Custom parameters differ')
            require(not win.results,'Trial touched UI results')
            stable=m2.call('workbench_context')['context_token'];win.store_search.setText('A');win.filter_stores();win.store_search.clear()
            require(m2.call('workbench_context')['context_token']==stable,'Browsing invalidates preview')
            adopted=m2.call('adopt_trial',{'task_id':t2['task_id'],'context_token':stable})
            require(adopted['adopted'] and win.results['A']['rows']==oracle['rows'],'Trial adoption wrong')
            require(win.config_for('A')['warehouses']['山东仓'][0]==10,'Custom parameter not adopted')
            require(win.work_repository.stores()==settings_before and not win.work_repository.list_jobs() and not win.orders.headers(),'Formal business data changed')
            report['checks'].append('Custom parameter trial exact equality; browsing stable; explicit adoption updates current workbench only, not defaults/drafts/orders')
            p3=prepare('manual-guard',mode='current')
            win.results['A']['preissue']={'synthetic':'manual-edit'}
            rejected=m2.call('run_calculation',{'preview_id':p3['task_id']},error=True)
            require(rejected['code']=='STALE_CONTEXT','Manual edit not invalidating')
            p4=prepare('manual-impact',mode='current')
            rejected=m2.call('run_calculation',{'preview_id':p4['task_id']},error=True)
            require(rejected['code']=='NEEDS_REVIEW' and 'A' in rejected['details']['manual'],'Missing manual review')
            win.results['A'].pop('preissue')
            report['checks'].append('Manual adjustments invalidate old requests and require original UI review before recalculation')
            win.concurrency.setValue(1)
            p5=prepare('cancel');t5=m2.call('run_calculation',{'preview_id':p5['task_id']})
            m2.call('stop_remaining',{'task_id':t5['task_id']});wait(lambda:win.automation.worker is None,60)
            stopped=m2.call('task_status',{'task_id':t5['task_id']})
            retry=[s['code'] for s in stopped['stores'] if s['state'] in ('已停止','计算失败')]
            require(retry,'Cancellation did not stop any queued store')
            retryargs={'task_id':t5['task_id'],'stores':retry,'operation_key':'retry-cancel'}
            t6=m2.call('retry_failed',retryargs);require(m2.call('retry_failed',retryargs)['task_id']==t6['task_id'],'Retry duplicate')
            wait(lambda:win.automation.worker is None,60)
            require(m2.call('task_status',{'task_id':t5['task_id']})['state']=='completed','Retry did not join original batch')
            require([s['store'] for s in m2.call('calculation_results',{'task_id':t5['task_id']})['stores']]==['B','A'],'Retry order changed')
            report['checks'].append('Actual serial spawn cancellation stops queued stores; retry has fresh cancellation state and rejoins original ordered batch')
            # Explicitly created QA archive, outside the AI surface, to exercise immutable history reading.
            orders=win.orders.confirm(win.snapshot(),['A'],None,{'A':None})
            from archive_orders import number
            h=m2.call('explain_decision',{'order_id':number(orders[0]['number'],'山东仓'),'store':'A','sku':'1001'})
            wait(lambda:win.automation.worker is None)
            value=m2.call('task_status',{'task_id':h['task_id']})
            require(value.get('explanation',{}).get('order',{}).get('revision')==orders[0]['revision'],str(value))
            require('explanation' not in win.automation.journal.get(h['task_id']),'History detail persisted to parallel store')
            report['checks'].append('Exact historical revision explanation uses original snapshot; task journal does not duplicate business rows')
            from automation_gateway import Gateway
            from automation_protocol import CommandError
            second=Gateway(win)
            try:second.enable();raise AssertionError('Second owner enabled')
            except CommandError as exc:require(exc.code=='BUSY','Wrong owner error')
            report['checks'].append('Second owner of same data environment rejected')
            from openpyxl import Workbook
            import store_parameter_import as spi
            def book(path,headers,rows):
                b=Workbook();b.active.append(headers)
                for row in rows:b.active.append(row)
                b.save(path);b.close()
            sales=out/'合成补货建议.xlsx';master=out/'合成商品资料.xlsx'
            book(sales,['仓库/门店编码','仓库/门店名称','商品编码','商品名称','日销','周销','月销','可用库存','在途库存','缺货天数（天）','基础单位','补货状态'],
                 [['B','测试B','1001','测试商品',2,14,60,3,0,0,'个','正常'],['A','测试A','1001','测试商品',2,14,60,3,0,0,'个','正常']])
            book(master,['商品编码','商品名称','分类','出库箱规','基本售价','商品状态','仓归属','商品标签'],[['1001','测试商品','休闲食品',1,8,'启用','山东仓','']])
            defaults={s:spi.base() for s in ('A','B')}
            for r in defaults.values():r['ui'].update(mode=1,primary='山东仓')
            win.work_repository.save_stores(defaults)
            win.reset_job();win.master_legacy.setChecked(True);win.products.set_path(str(master));win.auto_check.stop()
            fresh=prepare('new-file',mode='current',paths=[str(sales)])
            ft=m2.call('run_calculation',{'preview_id':fresh['task_id']});wait(lambda:win.automation.worker is None,60)
            state=m2.call('task_status',{'task_id':ft['task_id']})
            require(state.get('adopted') and list(win.loaded['stores'])==['B','A'],str(state))
            for s in ('B','A'):
                expected=batch_service.calculate_store(win.loaded,s,win.selected_settings(s),win.profile)
                require(win.results[s]['rows']==expected['rows'],'New input adoption differs '+s)
            # Verify original retention through the established source_files table.
            with win.work_repository.connect() as db:
                require(db.execute('SELECT count(*) FROM source_files').fetchone()[0]>=2,'New inputs were not retained')
            report['checks'].append('Actual XLSX check, current-mode calculation, source retention and fresh workbench adoption preserve file/store/row order and exact business values')
            from automation_ui import results as result_dialog
            from PySide6.QtWidgets import QApplication, QDialog, QTableWidget
            ui_seen=[]
            def inspect_result_dialog():
                dialog=QApplication.activeModalWidget()
                if isinstance(dialog,QDialog):
                    tables=dialog.findChildren(QTableWidget)
                    ui_seen.append(bool(tables and tables[0].rowCount() and tables[0].horizontalHeaderItem(3).text()=='执行数量'))
                    dialog.grab().save(str(out/'AI任务结果.png'));dialog.reject()
            QTimer.singleShot(500,inspect_result_dialog);result_dialog(win,ft['task_id'])
            require(ui_seen==[True],'Result UI did not show business rows')
            report['checks'].append('Actual result dialog shows Chinese business fields and generated rows; close does not mutate results')
            configs = connection_configs(win); require('mcp_servers.replenishment' in configs['codex'], 'Missing Codex config')
            require('replenishment' in configs['workbuddy']['mcpServers'], 'Missing WorkBuddy config')
            (out / 'clients.json').write_text(json.dumps(configs, ensure_ascii=False, indent=2), encoding='utf-8')
            if os.environ.get('REPLENISHMENT_CLIENT_RUNNER'):
                runner=Path(os.environ['REPLENISHMENT_CLIENT_RUNNER'])
                client_python=os.environ['REPLENISHMENT_CLIENT_PYTHON']
                with (out/'codex-client.log').open('w',encoding='utf-8') as log:
                    child=subprocess.Popen([client_python,str(runner),'--config',str(out/'clients.json'),'--out',str(out/'codex-client')],stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW)
                    wait(lambda:child.poll() is not None,180)
                require(child.returncode==0,(out/'codex-client.log').read_text('utf-8'))
                report['checks'].append('Installed Codex app-server discovers current tools and calls current workbench, paged tasks and validation error; ephemeral protocol context, no model turn')
            from automation_playbook_smoke import exercise
            recipes = exercise(app, win, m2, out, wait, require)
            require(recipes['passed'], 'Business recipes failed')
            report['checks'].append('Ten business recipes replayed through actual MCP; modeless picker, scoped prompt copy and stale-context/explicit-adoption guards')
            from automation_refresh_smoke import exercise as refresh_exercise
            refreshed = refresh_exercise(win, m2, out, wait, require)
            require(refreshed['passed'], 'CLI and read-only query acceptance failed')
            report['checks'].extend(refreshed['checks'])
            win.grab().save(str(out / 'AI协助工作台.png'))
            report['passed'] = True
        except Exception: report['error'] = traceback.format_exc()
        finally:
            for c in clients:
                try: c.close()
                except Exception: c.process.kill(); c.log.close()
            (out / 'automation-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            win._discard_close = True; win.job_dirty = False
            win.close()
            try:wait(lambda:win.aoxiang_workflows.session.client is None and win.aoxiang_workflows.session_closer is None and win.aoxiang_workflows.owner is None)
            except Exception:report['passed']=False;report['shutdown_error']=traceback.format_exc()
            (out / 'automation-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            QTimer.singleShot(100,lambda:app.exit(0 if report['passed'] else 1))
    QTimer.singleShot(100, run)
