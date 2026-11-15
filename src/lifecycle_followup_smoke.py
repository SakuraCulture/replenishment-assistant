"""Opt-in real windows/threads/files with a strict no-network transport."""
import copy,hashlib,json,os,sqlite3,sys,time,traceback
from contextlib import closing
from pathlib import Path
from PySide6.QtCore import QTimer,Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QMessageBox

def start_smoke(app,win,args):
    out=Path(args[0]).resolve();mode=args[1];marker=json.loads((out/'synthetic.json').read_text('utf-8'))
    assert marker['synthetic'] and not marker['live_platform']
    assert Path(os.environ['REPLENISHMENT_DATA_DIR']).resolve()==out/'settings' and win.work_repository.path.resolve()==out/'settings/workbench.sqlite3'
    win.auto_check.stop();win.jst_generator.stopped=True;win.jst_generator.timer.stop();win.aoxiang_workflows.stopping=True;win.aoxiang_workflows.timer.stop()
    import order_lifecycle as lc,jst_orders as jo,jst_results as jr,jst_payment as jp,work_repository as wr
    import aoxiang_purchase as ax,aoxiang_purchase_tasks as tasks
    from purchase_io import number
    from lifecycle_ui import Review
    from lifecycle_home_ui import StageDialog
    from aoxiang_purchase_ui import PurchaseDialog
    from aoxiang_ui_common import component_root
    path=win.work_repository.path;fixture=json.loads((out/'fixture.json').read_text('utf-8'));heads=fixture['heads'];config=fixture['configuration']
    suppliers={'河北仓':'2078246','山东仓':'ZXC2025'};checks=[];dialogs=[];messages=[];ticks=[];calls=[];action=['cancel'];reviews=[]
    def require(ok,text):
        if not ok:raise AssertionError(text)
        checks.append(text)
    def wait(predicate):
        until=time.monotonic()+45
        while not predicate() and time.monotonic()<until:QTest.qWait(20)
        if not predicate():raise AssertionError('等待实际界面超时')
    def facts(tables=None):
        result={}
        with closing(sqlite3.connect(path)) as db:
            names=tables or [n for n, in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name") if n.startswith(('lifecycle_','jst_','normal_'))]
            for table in names:
                h=hashlib.sha256()
                for row in db.execute('SELECT * FROM "'+table+'" ORDER BY rowid'):
                    h.update(wr.dumps([{'blob':v.hex()} if isinstance(v,bytes) else v for v in row]).encode());h.update(b'\n')
                result[table]=h.hexdigest()
        return result
    def tick():
        ticks.append(time.monotonic());dialog=app.activeModalWidget()
        if isinstance(dialog,Review) and not dialog.property('followupTestDriven'):
            dialog.setProperty('followupTestDriven',True);reviews.append([r[0] for r in dialog.overview_model.rows])
            require(not dialog.save.isEnabled(),'付款仍需明确核对确认后才能保存')
            dialog.grab().save(str(out/(mode+'-review-'+action[0]+'.png')))
            if action[0]=='cancel':dialog.reject()
            else:dialog.complete.setChecked(True);dialog.extra_ack.setChecked(True);QTest.mouseClick(dialog.save,Qt.MouseButton.LeftButton)
        elif isinstance(dialog,QMessageBox):
            messages.append(dialog.text()+'\n'+dialog.informativeText()+'\n'+dialog.detailedText());dialog.reject()
    timer=QTimer(win);timer.setInterval(20);timer.timeout.connect(tick)
    class NoNetwork:
        running=False
        def __init__(self,*args,**kwargs):pass
        def start(self):self.running=True
        def close(self,**kwargs):self.running=False;return True
        def request(self,method,**kwargs):
            calls.append(method)
            if method=='configuration':return copy.deepcopy(config)
            if method=='purchase.list':return {'items':[],'total':0}
            raise AssertionError('Offline EXE acceptance forbids platform operation: '+method)
    def purchase(head,index):
        before=facts();expected=ax.freeze_payment(path,head,config,suppliers)
        d=PurchaseDialog(win,win,head,client_factory=NoNetwork);dialogs.append(d);d.show();wait(lambda:d.configuration is not None and d.worker is None)
        def operation(progress):
            package=tasks.prepare(path,head,config,suppliers,fixture['template'],d.folder/'purchase-packages',component_root()/'assets/purchase-import-template-contract.json',progress=progress)
            intent=package['intent']
            record={'id':intent['id'],'state':'RESERVED','active':False,'uploadAttempted':False,'createdAt':wr.now(),'sequence':1,
                    'rows':len(intent['rows']),'totalQuantity':intent['totalQuantity'],'totalAmount':intent['totalAmount'],'basis':intent['basis']}
            # This report explicitly distinguishes local UI preview from a real
            # component reservation. No platform or reservation is simulated as real.
            return package,record
        d.run(operation,d.package_ready);wait(lambda:d.worker is None)
        require(d.package is not None and d.basis_state['valid'],'真实后台线程完成采购准备并显示有效依据')
        p=json.loads(Path(d.package['plan_file']).read_text('utf-8'))
        require(p['payment']==expected['payment'] and p['rows']==expected['rows'],'采购文件冻结付款明细、金额、两位单价、舍入差额及顺序一致')
        require(d.table.rowCount()==len(expected['rows']) and d.table.item(0,0).text()==expected['rows'][0]['sku'],'实际采购预览表格显示完整商品和原顺序')
        require(tasks.validate_for_upload(path,d.package,config,suppliers),'原有采购文件、准入、当前付款及上传前检查仍通过')
        require(facts()==before,'采购准备不改变签发、回传、运营、付款、收货及库存事实')
        require(not p['create_external_order'] and p['post_creation_state']=='待提交','准备只生成本地文件，尚未创建平台采购单')
        d.grab().save(str(out/(mode+'-purchase-'+str(index)+'.png')));d.close();wait(lambda:d._can_close and d.worker is None)
        require(not d.client.running,'采购窗口正常关闭并释放本轮离线传输')
        return d.package
    def execute():
        timer.start();unsafe=False;page=None
        try:
            if mode=='exercise':
                before=facts();dialog=StageDialog(win.followup,'jst');dialogs.append(dialog);dialog.show();page=dialog.panel;wait(lambda:page.data is not None and not page.reading);page.timer.stop()
                require(len(page.records)==4,'实际分配结果页显示两店四仓单')
                QTest.mouseClick(page.payment_all_btn,Qt.MouseButton.LeftButton)
                require(not page.payment_receipt and facts()==before,'自然转付款取消后不保存任何一单')
                wait(lambda:not page.reading);first=page.table.topLevelItem(0).child(0);first.setCheckState(0,Qt.CheckState.Checked)
                action[0]='selected';QTest.mouseClick(page.payment_selected_btn,Qt.MouseButton.LeftButton)
                require(page.payment_receipt is not None and len(page.payment_receipt)==1,'实际勾选入口仅保存所选一张仓单')
                wait(lambda:not page.reading);action[0]='all';QTest.mouseClick(page.payment_all_btn,Qt.MouseButton.LeftButton)
                require(page.payment_receipt is not None and len(page.payment_receipt)==4,'实际当前筛选入口完成四单付款，无Excel搬运')
                wait(lambda:not page.reading);before=facts();action[0]='repeat';QTest.mouseClick(page.payment_all_btn,Qt.MouseButton.LeftButton)
                require(page.payment_receipt is not None and not any(r['changed'] for r in page.payment_receipt) and facts()==before,'自然转付款重复确认不新增版本、事件或库存占用')
                wait(lambda:not page.reading)
                for head in heads:
                    with closing(sqlite3.connect(path)) as db:gid=jo.active(db,head);doc=lc.current(db,head,'payment')
                    generated=jo.get(path,gid)
                    require([(r['sku'],r['base_quantity'],r['amount']) for r in doc['payload']['rows'] if float(r['base_quantity'])>0]==[(r['sku'],number(r['quantity']),number(r['amount'])) for r in generated['rows'] if float(r['quantity'])>0],'付款数量、金额按原规范文本与生成单逐行相等：'+head['number'])
                dialog.close();purchase(heads[0],1)
                (out/'expected-facts.json').write_text(json.dumps(facts()),'utf-8')
            else:
                require(facts()==json.loads((out/'expected-facts.json').read_text('utf-8')),'退出重开后四环节事实及库存不变')
                entries=jr.prepare_export_ids(path,fixture['gids'])['entries'];before=facts();plan=jp.prepare(path,entries)
                require(not any(r['changed'] for r in lc.commit(path,plan)) and facts()==before,'重开后自然转付款仍去重，不依赖旧进程证据')
                purchase(heads[0],2)
            require(set(calls)<= {'configuration','purchase.list'},'离线传输未发起登录、模板导出、采购上传或平台查询')
            require(not messages,'没有忽略或隐藏非预期错误窗口')
            require(len(ticks)>3,'自然转付款和采购准备期间Qt事件循环持续运行')
            report={'passed':True,'frozen_exe':bool(getattr(sys,'frozen',False)),'synthetic':True,'live_platform':False,
                    'checks':checks,'heartbeat_ticks':len(ticks),'dialogs_not_stubbed':True,'transport':'strict offline test adapter',
                    'component_reservation_tested':False,'reviews':reviews,'calls':calls,'messages':messages}
        except Exception:report={'passed':False,'checks':checks,'error':traceback.format_exc(),'messages':messages,'calls':calls}
        finally:
            timer.stop()
            for d in reversed(dialogs):
                if isinstance(d,PurchaseDialog):
                    d.close()
                    try:wait(lambda:d._can_close and d.worker is None)
                    except AssertionError:unsafe=True
                else:d.close()
        if unsafe:report.update(passed=False,error='Owned worker still running; retained without forced exit')
        (out/(mode+'-report.json')).write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
        if not unsafe:win._discard_close=True;app.exit(0 if report['passed'] else 1)
    QTimer.singleShot(100,execute)
