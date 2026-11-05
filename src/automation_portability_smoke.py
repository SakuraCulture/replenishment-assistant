"""Packaged acceptance: actual UI, launcher, bridge and pipe; isolated data only."""
import json,os,sys,time,traceback,hashlib
from pathlib import Path
from unittest.mock import patch
from PySide6.QtCore import QTimer,QProcess
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QMessageBox


def start_smoke(app,win,args):
    out=Path(args[0]).resolve();beats=[]
    def wait(test,seconds=90):
        end=time.monotonic()+seconds
        while test() and time.monotonic()<end:QTest.qWait(10)
        if test():raise AssertionError('QA operation timed out')
    def execute():
        timer=QTimer(win);timer.setInterval(25);timer.timeout.connect(lambda:beats.append(time.monotonic()));timer.start();dialogs=[]
        try:
            if Path(os.environ.get('REPLENISHMENT_DATA_DIR','')).resolve()!=out/'settings':raise ValueError('需要独立QA资料')
            win.auto_check.stop();win.jst_generator.stopped=True;win.jst_generator.timer.stop();win.aoxiang_workflows.stopping=True;win.aoxiang_workflows.timer.stop()
            from retry_profile_smoke import install
            import automation_connection as c
            from automation_connection_ui import create_dialog
            install(win,'B');win.auto_check.stop();win.concurrency.setValue(2);g=win.automation;g.enable();before=g.token()
            for index,client in enumerate(('codex','workbuddy')):
                d=create_dialog(win);dialogs.append(d);d.client_pick.setCurrentIndex(index)
                config=out/('客户端 空格/'+client+('.toml' if index==0 else '.json'));config.parent.mkdir(exist_ok=True)
                raw=b'[mcp_servers.other]\ncommand="keep"\n' if index==0 else b'{"mcpServers":{"other":{"command":"keep"}}}'
                config.write_bytes(raw);d.config_path.setText(str(config));d.show();app.processEvents()
                with patch.object(QMessageBox,'question',return_value=QMessageBox.StandardButton.Cancel):d.configure_button.click()
                if config.read_bytes()!=raw:raise AssertionError('Cancel wrote config')
                with patch.object(QMessageBox,'question',return_value=QMessageBox.StandardButton.Ok):d.configure_button.click()
                d.check_button.click();wait(lambda:not d.check_button.isEnabled())
                if '验证通过' not in d.result_label.text():raise AssertionError(d.result_label.text())
                d.grab().save(str(out/(client+'-configuration.png')));d.reject();dialogs.remove(d);d.deleteLater();QTest.qWait(20)
            entry=c.planned_entry(g)
            def rpc(tool,arguments=None,doctor=False):
                p=QProcess(win);p.start(entry['command'],entry['args']+(['doctor'] if doctor else []))
                if not p.waitForStarted(10000):raise AssertionError(p.errorString())
                if not doctor:
                    requests=[{'jsonrpc':'2.0','id':1,'method':'initialize','params':{'clientInfo':{'name':'QA actual MCP'},'protocolVersion':'2025-11-25'}},
                        {'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':tool,'arguments':arguments or {}}}]
                    p.write(('\n'.join(json.dumps(r,ensure_ascii=False) for r in requests)+'\n').encode('utf-8'));p.closeWriteChannel()
                wait(lambda:p.state()!=QProcess.ProcessState.NotRunning,60)
                data=bytes(p.readAllStandardOutput()).decode('utf-8');errors=bytes(p.readAllStandardError()).decode('utf-8',errors='replace');p.deleteLater()
                if not data:raise AssertionError('No output: '+errors)
                return json.loads(data) if doctor else json.loads(data.splitlines()[-1])['result']
            if not rpc('',doctor=True)['result']['versions_match']:raise AssertionError('Version mismatch')
            ctx=rpc('workbench_context')['structuredContent']
            if ctx['instance_id']!=g.instance or ctx['workspace_id']!=g.workspace:raise AssertionError('Wrong context')
            g.disable()
            if rpc('',doctor=True)['error']['code']!='NOT_CONNECTED':raise AssertionError('Closed workspace accepted')
            g.enable()
            if not rpc('',doctor=True)['ok']:raise AssertionError('Re-enable failed')
            path=c.binding_path(g);correct=path.read_bytes();wrong=json.loads(correct);wrong['workspace']='other'
            path.write_text(json.dumps(wrong),'utf-8')
            if rpc('',doctor=True)['error']['code']!='WORKSPACE_MISMATCH':raise AssertionError('Wrong workspace accepted')
            path.write_bytes(correct)
            task=g.start_stock_diagnosis({'operation_key':'0400-exe','context_token':g.token(),'stores':['S1','S2'],'categories':['快销']});wait(lambda:g.worker is not None)
            if g.task_record(task['task_id'])['state']!='completed':raise AssertionError(g.task_record(task['task_id']))
            risks=rpc('stock_diagnosis_results',{'task_id':task['task_id'],'limit':100})['structuredContent']
            params=rpc('stock_diagnosis_parameters',{'task_id':task['task_id'],'candidate':1,'stores':['S1','S2']})['structuredContent']
            if risks['risk_count']!=0 or risks['risk_summary']['after_gap_skus']!=4:raise AssertionError('Risk meaning changed')
            if params['adopted'] or not params['changes'] or before!=g.token():raise AssertionError('Trial changed work')
            from version_info import diagnostic
            answer={'passed':True,'frozen_exe':bool(getattr(sys,'frozen',False)),**diagnostic(),
                'workspace_unchanged':before==g.token(),'tools':23,'risk_summary':risks['risk_summary'],
                'candidate_stores':[s['store'] for s in params['candidate_parameters']['stores']],
                'operations':['Codex配置取消/确认','WorkBuddy配置取消/确认','保留其他连接','配置检查按钮','实际稳定启动器','实际接口stdio','启停与重连','错误环境拒绝','双进程试算','候选完整参数','未采用'],
                'maximum_heartbeat_gap_seconds':max((b-a for a,b in zip(beats,beats[1:])),default=0),
                'limits':'本机合成资料，确认弹框由QA自动选择；不是另一台电脑或外部AI自然语言验收。'}
        except Exception:answer={'passed':False,'error':traceback.format_exc(),'frozen_exe':bool(getattr(sys,'frozen',False))}
        finally:
            for d in dialogs:d.reject();d.deleteLater()
        timer.stop();(out/'portability-exe.json').write_text(json.dumps(answer,ensure_ascii=False,indent=2,default=str),'utf-8')
        win._discard_close=True;win.job_dirty=False;app.exit(0 if answer['passed'] else 1)
    QTimer.singleShot(250,execute)
