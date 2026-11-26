"""Drive the delivered business widgets with isolated inputs and controlled external replies."""
import json
import os
import sys
import time
import traceback
from contextlib import ExitStack
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from PySide6.QtCore import QTimer,Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication,QCheckBox,QDialogButtonBox,QFileDialog,QInputDialog,QMessageBox,QPushButton

import special_plans as sp
from special_ui import Desk,Editor

def write(f,v):
    f.write_text(json.dumps(v,ensure_ascii=False,indent=2),'utf-8')

class Drive:
    def __init__(self,app,win,root):
        self.app,self.win,self.root=app,win,root
        self.log=[]
        self.file=''
        self.export=''
        self.cv=[]
        self.errors=[]
        self.measure={}
        self.counter=0
        self.screen={}
    def log_action(self,text):
        self.log.append([time.time(),text])
        with (self.root/'ui-events.jsonl').open('a',encoding='utf8') as s:s.write(json.dumps(self.log[-1],ensure_ascii=False)+'\n')
    def press(self,w,label):
        b=next((x for x in w.findChildren(QPushButton) if x.text()==label and x.isVisible()),None)
        if not b or not b.isEnabled():raise ValueError('操作入口未启用 '+label+' '+getattr(w,'status',w).text() if hasattr(w,'status') else label)
        self.log_action(w.windowTitle()+' / '+label)
        QTest.mouseClick(b,Qt.MouseButton.LeftButton)
        self.app.processEvents()
    def wait(self,pred,sec=20):
        until=time.monotonic()+sec
        while not pred():
            if self.errors:raise ValueError(self.errors[-1])
            if time.monotonic()>until:raise TimeoutError('等待窗口或原任务超时')
            QTest.qWait(20)
    def snap(self,d,title):
        self.counter+=1
        f=self.root/(f'{self.counter:03}-'+title+'.png')
        if not d.grab().save(str(f)):raise IOError(str(f))
        self.screen[title]=str(f)
    def desk(self,kind,enable=True):
        before={id(w) for w in self.app.topLevelWidgets() if isinstance(w,Desk)}
        self.press(self.win,sp.KINDS[kind])
        matches=[w for w in self.app.topLevelWidgets() if isinstance(w,Desk) and w.isVisible() and w.kind==kind and id(w) not in before]
        if not matches:raise ValueError('未打开业务工作台')
        d=matches[-1]
        if enable and not d.on.isChecked():QTest.mouseClick(d.on,Qt.MouseButton.LeftButton)
        return d
    def modal(self,action,work):
        fail=[]
        def inside():
            dlg=self.app.activeModalWidget()
            try:work(dlg)
            except BaseException:
                fail.append(traceback.format_exc())
                if dlg:dlg.reject()
        QTimer.singleShot(25,inside)
        action()
        if fail:raise RuntimeError(fail[0])
    def publish(self,d,raw,rev=False):
        data=self.root/('draft-'+str(len(self.log))+'.json')
        write(data,raw)
        self.file=str(data)
        def edit(dlg):
            assert isinstance(dlg,Editor)
            self.press(dlg,'读取方案文件')
            if rev:dlg.reason.setText('隔离业务批准修订')
            self.press(dlg,'确认发布')
            if dlg.isVisible():raise ValueError(dlg.error.text())
        self.modal(lambda:self.press(d,'批准修订' if rev else '新建'+sp.KINDS[d.kind]),edit)
        if '待核' in d.status.text():raise ValueError(d.status.text())
        return d.key()
    def preview(self,d):
        d.tabs.setCurrentIndex(0)
        began=time.perf_counter()
        self.press(d,'预览本轮')
        assert d.view,d.status.text()
        self.measure.setdefault('preview_render',[]).append(time.perf_counter()-began)
        return d.view['view']
    def claim(self,d):
        began=time.perf_counter()
        self.press(d,'确认本轮分配')
        self.wait(lambda:d.claim_worker is None,90)
        self.measure.setdefault('claim_and_render',[]).append(time.perf_counter()-began)
        if '已登记' not in d.status.text():raise ValueError(d.status.text())
    def proof(self,d,i,qty=None,final=False,status='已付款待审核'):
        d.tabs.setCurrentIndex(1)
        d.orders.selectRow(i)
        p=d.packages[i]
        if p['online'] not in self.jst.orders:
            self.jst_window(d,i)
        original=self.jst.orders[p['online']]
        if qty is not None:
            for item in original['items']:
                item.update(qty=str(qty),amount=str(Decimal(qty)*Decimal(item['price'])))
            original['amount']=str(sum(Decimal(v['amount']) for v in original['items']))
            if not final and status!='已取消':self.jst_window(d,i,recover_only=True)
        if final or status=='已取消':
            original.update(native_status='已取消' if status=='已取消' else '已发货',status='review' if status=='已取消' else 'ordered')
            original['final_allocations']=[dict(sku=item['sku'],unit=item['unit'],quantity=str(qty),outstanding='0',
                reference=p['online']+'-终结核对') for item in p['items']]
            self.jst_window(d,i,recover_only=True)
        rows=[]
        for item in p['items']:
            actual=item['quantity'] if qty is None else str(qty)
            row={'sku':item['sku'],'unit':item['unit'],'quantity':actual,'amount':str(Decimal(actual)*Decimal(item['price']))}
            if final:row['final_evidence']='隔离原单结束逐SKU凭证 '+row['sku']
            rows.append(row)
        obj={**p['scope'],'gid':p['gid'],'online':p['online'],'internal':original['internal'],
             'status':original['native_status'],'task':p['gid'],'rows':rows}
        f=self.root/('JST-'+p['oid']+'-'+str(len(self.log))+'.json')
        write(f,obj)
        self.file=str(f)
        d.tabs.setCurrentIndex(1)
        d.orders.selectRow(i)
        self.press(d,'核对聚水潭原件')
        if d.status.text().startswith('待核'):raise ValueError(d.status.text())
        return obj
    def jst_window(self,d,i,recover_only=False):
        d.tabs.setCurrentIndex(1)
        d.orders.selectRow(i)
        p=d.packages[i]
        started=time.perf_counter()
        def operate(w):
            from jst_auto_order_ui import Orders
            assert isinstance(w,Orders),str(type(w))
            self.measure.setdefault('native_prepare_render',[]).append(time.perf_counter()-started)
            assert [r['online'] for r in w.selected()]==[p['online']],w.note.text()
            def stage(name,label):
                t=time.perf_counter()
                self.press(w,label)
                self.measure.setdefault(name,[]).append(time.perf_counter()-t)
            if not recover_only:
                self.export=str(self.root/(p['online']+'.xls'))
                stage('write_xls','保存核对用 XLS')
                assert Path(self.export).is_file(),w.note.text()
                stage('platform_preview','平台预览')
                assert w.buttons['submit'].isEnabled(),str(w.note.text())
                stage('confirmed_import','确认导入')
            stage('original_readback','核对原单')
            rec=w.selected()[0]
            assert rec['internal']==self.jst.orders[p['online']]['internal'],str(w.note.text())
            if i==0:self.snap(w,'聚水潭原任务'+str(len(self.log)))
        self.platform_dialog(d,'聚水潭原任务',operate)
    def pay(self,d,i):
        d.tabs.setCurrentIndex(1)
        d.orders.selectRow(i)
        self.press(d,'人工确认付款')
        if d.status.text().startswith('待核'):raise ValueError(d.status.text())
    def load_stock(self,d):
        self.file=str(self.root/'inputs/山东仓-第二版.xlsx')
        self.press(d,'采用仓资料')
        assert d.status.text().startswith('已采用'),d.status.text()
    def inputs(self,name):
        return json.loads((self.root/'inputs'/(name+'.json')).read_text('utf8'))
    def output(self,d,name):
        d.tabs.setCurrentIndex(2)
        self.export=str(self.root/(name+'.csv'))
        self.press(d,'导出逐行对账')
        assert Path(self.export).is_file(),d.status.text()

    def exercise(self,case):
        from aoxiang_ui_common import ComponentDialog
        from special_qa_platform import Platform
        controlled=Platform(self.root)
        from special_qa_platform import JST
        from types import SimpleNamespace
        self.jst=JST(self.root)
        qa=self
        def initialize(d,host,factory,**kw):
            d.client=controlled
            d.client_factory=lambda *a:controlled
            d._control_only=False
            d._session_ticket=None
        def question(parent,title,message,*args):
            # Keep the real modal business prompt and click its affirmative button.
            qa.log_action(title+' / '+message[:250])
            def yes():
                box=qa.app.activeModalWidget()
                if isinstance(box,QMessageBox):QTest.mouseClick(box.button(QMessageBox.StandardButton.Yes),Qt.MouseButton.LeftButton)
            QTimer.singleShot(20,yes)
            return original_question(parent,title,message,*args)
        original_question=QMessageBox.question
        with ExitStack() as stack:
            stack.enter_context(patch.object(QFileDialog,'getOpenFileName',side_effect=lambda *a,**k:(self.file,'')))
            stack.enter_context(patch.object(QFileDialog,'getSaveFileName',side_effect=lambda *a,**k:(self.export,'')))
            stack.enter_context(patch.object(QMessageBox,'question',side_effect=question))
            stack.enter_context(patch.object(QInputDialog,'getItem',return_value=('山东仓',True)))
            stack.enter_context(patch.object(ComponentDialog,'init_client',initialize))
            stack.enter_context(patch('catalog_session_ui.manager',return_value=SimpleNamespace(session=self.jst)))
            self.controlled=controlled
            if case in ('opening','short','reopen'):return self.opening(case)
            if case.startswith('performance-'):return self.performance(int(case.split('-')[1]))
            if case.startswith('boundary-'):return self.boundary(int(case.split('-')[1]))
            if case.startswith('policy-') and case!='policy-receipts':return self.policy(case[7:])
            from special_scenarios import exercise
            return exercise(self,case)

    def policy(self,case):
        d=self.desk('policy')
        if case in ('fair','priority'):
            self.publish(d,self.inputs(case))
            view=self.preview(d)
            actual=[r['quantity'] for r in view['rows']]
            assert actual==(['3','2','2'] if case=='fair' else ['4','3','0'])
            self.snap(d,'分配预览')
            self.claim(d)
            self.load_stock(d)
            second=[r['quantity'] for r in self.preview(d)['rows']]
            assert second==(['1','2','2'] if case=='fair' else ['0','1','4']),second
            self.output(d,'逐店对账')
            return dict(first=actual,second=second)
        if case=='hold':
            raw=self.inputs('hold')
            raw['maintain']=True
            self.publish(d,raw)
            actual=[]
            for i,overlap in enumerate((2,0,1,None,0)):
                obs=dict(store='A',sku='HOLD',at=f'2026-11-26T10:00:0{i}+08:00',snapshot='S'+str(i),
                         available=3 if i!=4 else None,transit=4,promise=2,included=overlap,evidence='库存快照与承诺对照')
                f=self.root/('snapshot-'+str(i)+'.json');write(f,[obs]);self.file=str(f)
                self.press(d,'登记门店快照')
                row=self.preview(d)['rows'][0]
                actual.append([row['quantity'],row['observed'],row['reason']])
            assert [v[0] for v in actual]==['3','1','2','0','0'] and all(v[1]=='7' for v in actual[:4]),actual
            self.snap(d,'持有快照缺项')
            return dict(results=actual)
        if case=='boxes':
            raw=self.inputs('boxes')
            self.publish(d,raw)
            first=self.preview(d)['rows']
            assert [r['quantity'] for r in first]==['0','10','0','0','0'],first
            self.snap(d,'包装准入')
            raw['lines'][0].update(round_up=True,round_approval='测试业务整箱批准')
            self.publish(d,raw,True)
            after=self.preview(d)['rows'][0]
            assert (after['quantity'],after['extra'])==('12','2'),after
            return {'first':[[r['sku'],r['quantity'],r['reason']] for r in first],'approved':[after['quantity'],after['extra']]}
        if case=='scope':
            raw=self.inputs('scope')
            key=self.publish(d,raw)
            rows=self.preview(d)['rows'];assert {r['store'] for r in rows}=={'A','B'}
            def reply(q,v):
                f=self.root/'reply.json';write(f,dict(plan=key,version=v,store='A',quantities=q));self.file=str(f)
                self.press(d,'门店确认')
                return d.status.text()
            a=[r for r in rows if r['store']=='A']
            assert '款数' in reply({a[0]['id']:2},1)
            valid={a[0]['id']:2,a[1]['id']:2}
            assert reply(valid,1)==reply(valid,1)
            assert '版本' in reply(valid,0)
            assert '已保存' in reply({},1)
            final=self.preview(d)['rows']
            assert all(r['confirmation']==2 if r['store']=='A' else r['confirmation'] is None for r in final)
            self.snap(d,'门店确认')
            return dict(stores=['A','B'],reply_a=2,reply_b=None,rows=len(final))
        if case=='part':
            raw=self.inputs('fair');raw.update(stores=['A'],lines=[dict(sku='PART',unit='件',target='40')])
            self.publish(d,raw);self.preview(d);self.claim(d)
            self.proof(d,0,30,False)
            no=self.preview(d)['rows'][0]
            self.proof(d,0,30,True)
            yes=self.preview(d)['rows'][0]
            self.claim(d)
            self.proof(d,0,30,True)
            last=self.preview(d)['rows'][0]
            assert [r['quantity'] for r in (no,yes,last)]==['0','10','0']
            self.output(d,'原40实配30对账')
            return dict(before=no['quantity'],after=yes['quantity'],replay=last['quantity'],packages=len(d.packages))
        raise ValueError(case)

    def boundary(self,n):
        d=self.desk('opening')
        raw=self.inputs('opening')
        raw['lines']=[dict(sku=f'{i:06}',target='1',unit='件') for i in range(1,n+1)]
        # Use an independent full-stock publication for each boundary.
        self.load_stock(d)
        if not n:raw['lines']=[dict(sku='000001',target='0',unit='件')]
        self.publish(d,raw)
        v=self.preview(d)
        sizes=[len(p['items']) for p in v['packets']]
        assert sizes==[min(800,n-i) for i in range(0,n,800)]
        if n:
            self.claim(d)
            assert [i['sku'] for p in d.packages for i in p['items']]==[f'{i:06}' for i in range(1,n+1)]
        self.snap(d,'拆包边界')
        return dict(sizes=sizes,source_count=n)

    def performance(self,n):
        d=self.desk('opening');self.load_stock(d)
        raw=self.inputs('opening');self.publish(d,raw);d.limit.setValue(n)
        v=self.preview(d);self.claim(d)
        out=[]
        d.tabs.setCurrentIndex(1)
        for i,p in enumerate(d.packages):
            d.orders.selectRow(i)
            self.jst_window(d,i)
            out.append(len(p['items']))
        self.output(d,'货盘对账')
        assert out==[min(n,1650-i) for i in range(0,1650,n)]
        return dict(limit=n,sizes=out,schemes=self.measure)

    def platform_dialog(self,d,entry,work):
        def operate(dialog):
            try:work(dialog)
            finally:
                if dialog and dialog.isVisible():
                    self.wait(lambda:not getattr(dialog,'worker',None) and not getattr(dialog,'busy',False))
                    dialog.close()
        self.modal(lambda:self.press(d,entry),operate)

    def ax_flow(self,d,size,amount):
        def purchase(w):
            self.wait(lambda:not w.worker and w.preview_btn.isEnabled())
            self.press(w,'生成采购预览（获取最新模板）')
            self.wait(lambda:not w.worker and w.upload_btn.isEnabled(),35)
            assert len(w.package['intent']['rows'])==size and Decimal(w.package['intent']['totalAmount'])==Decimal(amount)
            self.snap(w,'原生模板1650款')
            self.press(w,'上传建单（保留待提交）')
            self.wait(lambda:not w.worker and self.controlled.state['accepted']==1,35)
            if (self.root/'lose-ax-response').exists():
                self.press(w,'刷新本单记录')
                self.wait(lambda:not w.worker and w.verify_btn.isEnabled(),35)
                self.press(w,'继续核对原采购任务')
            self.wait(lambda:not w.worker and (w.current or {}).get('state')=='BOUND')
            self.snap(w,'原翱象采购')
        self.platform_dialog(d,'翱象采购与收货',purchase)
        d.tabs.setCurrentIndex(2)
        def receipt(w):
            self.wait(lambda:not w.worker and w.sync_btn.isEnabled(),30)
            self.press(w,'立即同步 / 继续原下载任务')
            if (self.root/'lose-receipt-response').exists():
                self.wait(lambda:self.controlled.state.get('dropped') and not w.worker,35)
                self.press(w,'刷新本地记录')
                self.wait(lambda:not w.worker and w.save_btn.isEnabled() and w.posted_receipt,35)
                self.snap(w,'正式入账后补回执')
                return
            self.wait(lambda:not w.worker and (w.current or {}).get('state')=='SYNCED',35)
            self.snap(w,'原采购实收')
        self.platform_dialog(d,'回读原采购实收',receipt)
        if (self.root/'lose-receipt-response').exists():
            d.tabs.setCurrentIndex(1);d.orders.selectRow(0)
            p=d.packages[0]
            with sp.connection(d.path) as db:paid=sp.latest(db,p['oid'],'付款')
            v=paid['body']
            amounts={r['sku']:r['amount'] for r in v['rows']}
            first=next(iter(amounts));amounts[first]=str(Decimal(amounts[first])+Decimal('.01'))
            file=self.root/'仅更正原付款金额.json'
            write(file,dict(internal=v['internal'],online=p['online'],version=paid['version'],reason='回执中断后人工复核商品金额',amounts=amounts))
            self.file=str(file);self.press(d,'更正本包付款')
            assert not d.status.text().startswith('待核'),d.status.text()
            d.tabs.setCurrentIndex(2)
            def recover(w):
                self.wait(lambda:not w.worker and w.save_btn.isEnabled() and w.posted_receipt,50)
                self.press(w,'补发原实收确认')
                self.wait(lambda:not w.worker and (w.current or {}).get('state')=='SYNCED',50)
                self.snap(w,'改款后补回原回执')
            self.platform_dialog(d,'回读原采购实收',recover)

    def opening(self,case):
        d=self.desk('opening',enable=case!='reopen')
        if case=='reopen':
            d.tabs.setCurrentIndex(1)
            assert len(d.packages)==4
            d.orders.selectRow(3)
            d.tabs.setCurrentIndex(2)
            self.press(d,'查看组级收货')
            self.output(d,'重启对账')
            self.snap(d,'重启追溯')
            return dict(packages=len(d.packages),status=d.detail.toPlainText())
        self.publish(d,self.inputs('opening'))
        a=self.preview(d)
        assert [len(p['items']) for p in a['packets']]==[800,800,5]
        self.snap(d,'首次分配');self.claim(d)
        self.load_stock(d);self.preview(d);self.claim(d)
        counts=[len(p['items']) for p in d.packages]
        assert counts==[800,800,5,45]
        sums=[]
        for i in range(4):
            original=self.proof(d,i)
            sums.append(str(sum(Decimal(r['amount']) for r in original['rows'])))
            if i==3:
                self.press(d,'冻结合并采购')
                assert '人工付款确认' in d.status.text(),d.status.text()
            self.pay(d,i)
        assert [Decimal(v) for v in sums]==list(map(Decimal,['1600','1600','10','90']))
        self.snap(d,'四包原号与人工核款')
        self.press(d,'冻结合并采购')
        assert '3300.00' in d.status.text(),d.status.text()
        self.snap(d,'采购组')
        d.tabs.setCurrentIndex(1);d.orders.selectRow(3)
        self.ax_flow(d,1650,3300)
        self.press(d,'查看组级收货')
        self.output(d,'开业逐行对账')
        with sp.connection(d.path) as db:
            row=db.execute('SELECT original,group_id,version FROM special_receipts').fetchall()
            assert len(row)==1 and row[0][2]==1
            doc=sp.latest(db,row[0][1],'正式实收')
            qty=sum(Decimal(r['quantity']) for r in doc['body']['rows'])
            assert qty==(1649 if case=='short' else 1650)
        self.press(d,'全部经营实绩')
        metrics=json.loads(d.detail.toPlainText())
        assert metrics['全部']['实收件数']==str(qty) and metrics['普通']['实收件数']=='0',metrics
        write(self.root/'经营口径.json',metrics)
        self.snap(d,'组实收汇总')
        return dict(packages=counts,amounts=sums,original=row[0][0],received=str(qty),records=len(row),group=row[0][1],
                    creates=self.controlled.state['create'],accepted=self.controlled.state['accepted'],queries=self.controlled.state['queries'])

def start_smoke(app,win,args):
    root,case=Path(args[0]).resolve(),args[1]
    if (root/'settings')!=Path(os.environ.get('REPLENISHMENT_DATA_DIR','')).resolve() or win.work_repository.path.resolve()!=root/'settings/workbench.sqlite3':
        raise ValueError('专项实机验收必须使用隔离副本')
    from joint_export_smoke import isolated
    isolated(win,root)
    win.special_scheduler.stop()
    win.browser_status.paused=True
    win.browser_status.initial.stop()
    def execute():
        d=Drive(app,win,root)
        original=QMessageBox.exec
        # Timers handle real error dialogs from failed controlled replies.
        tick=QTimer(win)
        def dismiss():
            modal=app.activeModalWidget()
            if isinstance(modal,QMessageBox) and not modal.button(QMessageBox.StandardButton.Yes):
                d.log_action('界面提示 '+modal.windowTitle()+' '+modal.text()[:200])
                modal.accept()
        tick.timeout.connect(dismiss)
        tick.start(100)
        r=dict(case=case,frozen=bool(getattr(sys,'frozen',False)),exe=sys.executable)
        begin=time.perf_counter()
        try:r.update(actual=d.exercise(case),passed=True)
        except BaseException:r.update(passed=False,error=traceback.format_exc())
        finally:
            tick.stop()
            r.update(seconds=time.perf_counter()-begin,screens=d.screen,measure=d.measure)
            write(root/(case+'-ui.json'),r)
            for w in app.topLevelWidgets():
                if isinstance(w,Desk):w.close()
            win.job_dirty=False
            win._discard_close=True
            win.close()
    QTimer.singleShot(150,execute)
