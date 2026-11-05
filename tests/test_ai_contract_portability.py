import copy,json,os,sys,tempfile,tomllib,unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'src'),str(Path(__file__).parent)]
import automation_connection as c
from automation_protocol import CommandError,wire_default,validate_call
import history_diagnostics as h
from test_history_diagnostics import row,report


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)/'用户 空格/AI';self.env=patch.dict(os.environ,REPLENISHMENT_AI_HOME=str(self.root));self.env.start()
        self.g=SimpleNamespace(workspace='a'*24,connection_file=self.root/'settings/automation/connection.json')
    def tearDown(self):self.env.stop();self.temp.cleanup()
    def test_stable_registration_upgrade_move_and_no_secret(self):
        e=c.install_entry(self.g);p=c.binding_path(self.g);before=json.loads(p.read_text('utf-8'))
        moved=self.root/'新版 中文/bridge.exe';moved.parent.mkdir();moved.write_bytes(b'fixture')
        with patch.object(c,'bridge_command',return_value=(str(moved),[])):c.refresh_registration(self.g)
        self.assertEqual(c.planned_entry(self.g),e);data=json.loads(p.read_text('utf-8'));self.assertEqual(data['command'],str(moved))
        self.assertEqual(data['workspace'],before['workspace']);self.assertNotIn('token',data);self.assertNotIn('server',data)
        from automation_launcher import resolve
        self.assertEqual(resolve(p)[0],str(moved));moved.unlink()
        with self.assertRaisesRegex(ValueError,'移动'):resolve(p)
    def test_new_machine_gets_new_local_paths_and_explicit_workspace(self):
        a=c.install_entry(self.g)
        with patch.dict(os.environ,REPLENISHMENT_AI_HOME=str(self.root/'第二台')):
            other=SimpleNamespace(workspace='b'*24,connection_file=self.root/'第二台/资料/connection.json')
            b=c.install_entry(other)
            self.assertNotEqual(a,b);self.assertFalse(other.connection_file.exists())
            self.assertEqual(json.loads(c.binding_path(other).read_text('utf-8'))['workspace'],other.workspace)
    def test_config_merge_preserves_unrelated_and_private_backup(self):
        samples={'codex':b'# retained\nmodel="x"\n[mcp_servers.other]\ncommand="other"\n[mcp_servers.replenishment]\ncommand="old"\n[mcp_servers.replenishment.env]\nPRIVATE="unchanged-backup"\n[features]\nfoo=true\n',
            'workbuddy':b'{"mcpServers":{"other":{"env":{"PRIVATE":"keep"}},"replenishment":{"command":"old"}},"setting":true}'}
        for client,raw in samples.items():
            p=self.root/(client+'.config');p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(raw)
            entry=c.install_entry(self.g);plan=c.prepare_config(client,p,entry);receipt=c.apply_config(plan)
            self.assertEqual(Path(receipt['backup']).read_bytes(),raw)
            after=tomllib.loads(p.read_text("utf-8")) if client=='codex' else json.loads(p.read_text("utf-8"))
            key='mcp_servers' if client=='codex' else 'mcpServers'
            before=tomllib.loads(raw.decode()) if client=='codex' else json.loads(raw)
            self.assertEqual(after[key]['other'],before[key]['other']);self.assertEqual(after[key]['replenishment']['command'],entry['command'])
            self.assertFalse(c.apply_config(c.prepare_config(client,p,entry))['changed'])
    def test_quoted_toml_invalid_and_concurrent_edit(self):
        entry=c.planned_entry(self.g)
        raw=b'[mcp_servers."replenishment"]\ncommand="old"\n[mcp_servers."other"]\ncommand="keep"\n'
        out,_=c.merge_config('codex',raw,entry);self.assertEqual(tomllib.loads(out.decode())['mcp_servers']['other']['command'],'keep')
        for kind,text in [('codex',b'not toml'),('workbuddy',b'[]')]:
            with self.assertRaises(ValueError):c.merge_config(kind,text,entry)
        p=self.root/'c';p.parent.mkdir(parents=True);p.write_bytes(b'{}');plan=c.prepare_config('workbuddy',p,entry);p.write_bytes(b'{"other":1}')
        with self.assertRaisesRegex(ValueError,'其他操作'):c.apply_config(plan)
        self.assertEqual(p.read_bytes(),b'{"other":1}')
    def test_connection_errors_never_expose_secret_or_guess_environment(self):
        p=self.g.connection_file
        with self.assertRaises(CommandError) as e:c.load_connection(p,self.g.workspace)
        self.assertEqual(e.exception.code,'NOT_CONNECTED')
        p.parent.mkdir(parents=True);p.write_text(json.dumps({'protocol':'p','server':'s','token':'private-secret','workspace':'other'}),'utf-8')
        with self.assertRaises(CommandError) as e:c.load_connection(p,self.g.workspace)
        self.assertEqual(e.exception.code,'WORKSPACE_MISMATCH');self.assertNotIn('private-secret',str(e.exception))
        p.write_text('{','utf-8')
        with self.assertRaises(CommandError) as e:c.load_connection(p)
        self.assertEqual(e.exception.code,'INVALID_CONNECTION')
    def test_contextual_decimal_zero_is_not_unknown(self):
        value=json.loads(json.dumps([Decimal('0E+20'),Decimal('-0'),Decimal('1.2300'),None,0,''],default=wire_default))
        self.assertEqual(value,['0','0','1.2300',None,0,''])
    def test_evidence_dates_and_historical_parameters(self):
        first=row('2026-10-29',order='OLD',observations=dict(K=0,L=0,J=30,AN=1,AM=1,AG=8,AH=4))
        first['quantities'].update(model=24,signed=24,**{'return':12,'payment':6,'receipt':3})
        second=row('2026-11-01',order='NEW',observations=dict(K=0,L=0,J=30,AN=1,AM=1,AG=8,AH=4))
        before=copy.deepcopy([first,second]);r=h.review(report([first,second]))['items'][-1]
        self.assertEqual({e['business_date'] for e in r['evidence_details']},{'2026-10-29'})
        self.assertEqual({e['order_number'] for e in r['evidence_details']},{'OLD'})
        self.assertEqual(r['parameter_basis']['protection_days'],12)
        self.assertEqual(r['observation_basis']['order_number'],'NEW');self.assertEqual([first,second],before)


class LiveContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import test_automation_gateway as f
        f.GatewayRegression.setUpClass()
    def setUp(self):
        import test_automation_gateway as f
        self.f=f.GatewayRegression();self.f.setUp();self.g=self.f.g;self.f.win.automation=self.g
    def tearDown(self):self.f.tearDown()
    def test_complete_candidate_parameters_and_existing_risks(self):
        from retry_profile_smoke import install
        import stock_diagnosis
        install(self.f.win,'B');self.f.win.concurrency.setValue(1);token=self.g.token()
        s=stock_diagnosis.run(self.g.snapshot({}),{'stores':['S1'],'categories':['快销']},{})
        self.g.diagnosis_sessions={'diag':s}
        r=self.g.dispatch('stock_diagnosis_results',{'task_id':'diag'})
        self.assertEqual(r['risk_count'],0);self.assertEqual(r['risk_summary']['before_gap_skus'],2);self.assertEqual(r['risk_summary']['after_gap_skus'],2)
        p=self.g.dispatch('stock_diagnosis_parameters',{'task_id':'diag','candidate':1,'stores':['S1']})
        old=p['baseline']['stores'][0];new=p['candidate_parameters']['stores'][0]
        self.assertNotEqual(old['parameter_signature'],new['parameter_signature'])
        self.assertEqual(old['route_and_budget'],new['route_and_budget']);self.assertTrue(new['parameters']);self.assertTrue(p['changes'])
        self.assertFalse(p['adopted']);self.assertEqual(self.g.token(),token)
        with self.assertRaises(CommandError):self.g.dispatch('stock_diagnosis_parameters',{'task_id':'diag','candidate':1,'stores':['absent']})
        for args in ({'task_id':'diag','candidate':0,'stores':['S1']},{'task_id':'diag','candidate':1,'stores':[]}):
            with self.assertRaises(CommandError):validate_call('stock_diagnosis_parameters',args)
    def test_real_launcher_doctor_restart_and_wrong_environment(self):
        from PySide6.QtCore import QProcess
        from PySide6.QtTest import QTest
        import time
        entry=c.install_entry(self.g)
        def run(args):
            p=QProcess();p.start(entry['command'],entry['args']+args);end=time.monotonic()+20
            while p.state()!=QProcess.ProcessState.NotRunning and time.monotonic()<end:QTest.qWait(10)
            if p.state()!=QProcess.ProcessState.NotRunning:p.kill();p.waitForFinished();self.fail('launcher timed out')
            raw=bytes(p.readAllStandardOutput()).decode('utf-8');self.assertTrue(raw,bytes(p.readAllStandardError()).decode('utf-8',errors='replace'));return json.loads(raw)
        a=run(['doctor']);self.assertTrue(a['ok']);self.assertTrue(a['result']['versions_match'])
        self.assertEqual(a['result']['workbench']['workspace_id'],self.g.workspace)
        self.g.disable();self.assertEqual(run(['doctor'])['error']['code'],'NOT_CONNECTED')
        self.g.enable();self.assertTrue(run(['doctor'])['ok'])
        path=c.binding_path(self.g);data=json.loads(path.read_text('utf-8'));data['workspace']='other';path.write_text(json.dumps(data),'utf-8')
        self.assertEqual(run(['doctor'])['error']['code'],'WORKSPACE_MISMATCH')

    def test_ui_configure_cancel_install_check_and_close(self):
        from automation_connection_ui import create_dialog
        from PySide6.QtWidgets import QMessageBox
        from PySide6.QtTest import QTest
        from PySide6.QtCore import Qt
        import time
        d=create_dialog(self.f.win);d.client_pick.setCurrentIndex(1)
        path=Path(self.f.tmp.name)/'client/mcp.json';path.parent.mkdir();path.write_text('{"mcpServers":{"other":{"command":"keep"}}}','utf-8')
        d.config_path.setText(str(path));d.show();before=path.read_bytes()
        with patch.object(QMessageBox,'question',return_value=QMessageBox.StandardButton.Cancel):d.configure_button.click()
        self.assertEqual(path.read_bytes(),before)
        with patch.object(QMessageBox,'question',return_value=QMessageBox.StandardButton.Ok):d.configure_button.click()
        self.assertEqual(json.loads(path.read_text('utf-8'))['mcpServers']['other']['command'],'keep')
        d.check_button.click();end=time.monotonic()+20
        while not d.check_button.isEnabled() and time.monotonic()<end:QTest.qWait(10)
        self.assertIn('验证通过',d.result_label.text());d.reject();d.deleteLater();QTest.qWait(10)


if __name__=='__main__':unittest.main()
