"""Opt-in native Qt acceptance on a newly created synthetic database only."""
import copy,json,sys,time,traceback
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch
from PySide6.QtCore import QTimer,QEventLoop,Qt
from version_info import diagnostic

def start_smoke(app,win,args):
    out=Path(args[0]);checks=[];dialog=None
    def require(ok,title):
        if not ok:raise AssertionError(title)
        checks.append(title)
    def wait(fn):
        loop=QEventLoop();timer=QTimer();start=time.monotonic()
        timer.timeout.connect(lambda:loop.quit() if fn() or time.monotonic()-start>60 else None);timer.start(20);loop.exec();timer.stop()
        if not fn():raise AssertionError('等待隔离Qt任务超时')
    def execute():
        nonlocal dialog
        try:
            require('payment_matching' not in sys.modules,'启动未加载付款识别能力或扫描识别索引')
            path=win.work_repository.path;require(path.resolve()==(out/'settings/workbench.sqlite3').resolve(),'仅使用本轮隔离数据库')
            wait(lambda:win.jst_generator.worker is None);win.jst_generator.stopped=True
            import payment_matching as matching,order_lifecycle as lc
            from lifecycle_batch_ui import BatchImport
            from runtime_support import Cancelled
            from aoxiang_workflows import Workflows
            from aoxiang_receipt_scope import assess
            f=json.loads((out/'matching-fixture.json').read_text('utf-8'))
            dialog=BatchImport(win,win,'payment');dialog.show();dialog.add_paths([f['payment']])
            require(dialog.read_ready and len(dialog.documents)==1 and dialog.source_company.currentData() is None,'默认自动模式完整读取付款表，无需选择来源企业')
            require(dialog.targets[0].currentData()['number']==f['head']['number'],'未选择企业或目标单即完整识别原仓单')
            dialog.match_more.setChecked(True);dialog.match_from.setText('2099-01-01');dialog.filter_candidates()
            require(dialog.targets[0].currentData()['number']==f['head']['number'],'日期筛选保留已识别原单，唯一性不受窄日期影响')
            dialog.expand_candidates();app.processEvents();dialog.grab().save(str(out/'付款识别.png'))
            dialog.preview_btn.click()
            require(dialog.pages.currentIndex()>0 and not dialog.review.save.isEnabled(),'付款完整范围预览须人工确认才允许保存')
            dialog.review.grab().save(str(out/'付款核对.png'));dialog.review.complete.setChecked(True);dialog.review.extra_ack.setChecked(True);dialog.review.save.click()
            require(bool(dialog.receipt) and dialog.receipt[0]['changed'],'实际界面保存付款和内部单号对应')
            index=matching.build(path);result=matching.analyze(path,dialog.documents[0],index)
            require(result['automatic'],'重复导入自动识别原订单，无需来源企业')
            plan=matching.prepare(path,dialog.documents,None,index,None);require(not lc.commit(path,plan)[0]['changed'],'相同付款重导不增加付款版本')
            for case in f.get('automatic_cases',[]):
                probe=BatchImport(win,win,'payment');probe.show();probe.add_paths([case['path']])
                require(probe.read_ready and len(probe.documents)==1,'场景完整读取：'+case['name'])
                chosen=probe.targets[0].currentData()
                require((chosen['number'] if chosen else None)==case['expected_number'],'无需选择来源及订单：'+case['name'])
                if case.get('reason'):require(case['reason'] in probe.table.item(0,6).text(),'明确解释：'+case['name'])
                if case.get('screenshot'):app.processEvents();probe.grab().save(str(out/case['screenshot']))
                probe.close();probe.deleteLater()
            saved=lc.snapshot(path,f['head'])['documents']['payment'];basis={k:f['head'][k] for k in ('number','store','warehouse','oid','revision')};basis.update(paymentVersion=saved['version'],paymentDigest=saved['digest'])
            record={'id':'OFFLINE-PO','state':'BOUND','basis':basis,'target':{'displayName':'隔离合成门店'},'binding':{'orderId':'PO-OFFLINE','status':'待提交'}}
            store=Workflows(path.parent/'aoxiang',business_path=path);store.configure_schedules([record],5,True);store.observe_schedule(record['id'],{'state':'VOIDED','terminal':True,'terminalKind':'VOIDED','summary':{'status':'已作废','observedAt':'2026-11-08T00:00:00Z'}})
            schedule=store.schedule(record['id']);require(not schedule['enabled'] and not schedule['final_pending'] and not lc.snapshot(path,f['head'])['present']['receipt'],'作废停轮询，无最终实收待办，不写收货0')
            wrong=copy.deepcopy(record);wrong['id']='old';wrong['basis']['oid']='deleted-original'
            require(not assess(path,[wrong])['old']['eligible'],'失效本地原身份不能按同号现订单进入同步')
            from aoxiang_workflow_ui import ReceiptSelection
            void=copy.deepcopy(record);void['id']='void-local';void['binding']['orderId']='PO-VOID'
            class Client:
                running=False
                def __init__(self,*_):pass
                def start(self):self.running=True
                def close(self,timeout=10):self.running=False;return True
                def request(self,action,**kwargs):
                    if action=='purchase.list':return {'total':3,'items':[record,wrong,void]}
                    if action=='receipt.list':return {'total':1,'items':[{'id':void['id'],'state':'VOIDED','terminal':True,'summary':{'status':'已作废'}}]}
                    raise AssertionError('禁止平台动作：'+action)
            host=SimpleNamespace(repository=win.repository,work_repository=win.work_repository,aoxiang_workflows=SimpleNamespace(store=store,gate=win.aoxiang_workflows.gate,client_factory=None))
            selection=ReceiptSelection(win,host,[f['head']],client_factory=Client);selection.show();wait(lambda:selection.worker is None and len(selection.records)==1)
            require(selection.records[0]['id']==record['id'],'本批同步正常池排除失效原身份及作废采购单')
            selection.history_view.setChecked(True);wait(lambda:selection.worker is None and len(selection.records)==3);selection.select_visible()
            require([r['id'] for r in selection.selected()]==[record['id']],'历史只读记录不能勾选执行；未选采购不被带入')
            selection.grab().save(str(out/'同步历史只读.png'));selection.close();wait(lambda:selection._close_done);selection.deleteLater()
            dialog.deleteLater();dialog=BatchImport(win,win,'payment');dialog.show();dialog.add_paths([f['payment']])
            with patch('payment_matching.build',side_effect=Cancelled('isolated cancellation')):dialog.refresh_payment_index()
            require(not dialog.read_ready and not dialog.preview_btn.isEnabled(),'识别刷新取消后不能沿用过期预览保存')
            dialog.deleteLater();dialog=None
            result={'passed':True,'checks':checks,'live_platform':False,**diagnostic()}
        except Exception:result={'passed':False,'checks':checks,'live_platform':False,'error':traceback.format_exc(),**diagnostic()}
        (out/'report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),'utf-8')
        if dialog:dialog.close();dialog.deleteLater()
        app.exit(0 if result['passed'] else 1)
    QTimer.singleShot(300,execute)
