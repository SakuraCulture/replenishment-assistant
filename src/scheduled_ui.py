"""One scheduling entrypoint and focused task actions; no calculation implementation."""
import copy
import threading
from datetime import datetime
from PySide6.QtCore import QObject,QTimer,Qt
from PySide6.QtWidgets import (QDialog,QWidget,QVBoxLayout,QHBoxLayout,QFormLayout,QTabWidget,
    QTableWidget,QTableWidgetItem,QLineEdit,QComboBox,QSpinBox,QCheckBox,QMessageBox,QProgressBar,QTimeEdit,QDateEdit)
from ui_controls import label,button
from aoxiang_ui_common import Call
import scheduled_replenishment as sr
from scheduled_execution import freeze,check_signature,Runner,workbench_record,publish_catalogs
from scheduled_platforms import Platforms


def table(headers):
    t=QTableWidget(0,len(headers));t.setHorizontalHeaderLabels(headers)
    t.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers);t.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    t.setSelectionMode(QTableWidget.SelectionMode.SingleSelection);t.horizontalHeader().setStretchLastSection(True)
    return t


def fill(t,rows):
    t.setRowCount(len(rows))
    for i,row in enumerate(rows):
        for j,v in enumerate(row):t.setItem(i,j,QTableWidgetItem(str(v)))
    t.resizeColumnsToContents()


def ask(parent,title,text):
    return QMessageBox.question(parent,title,text,QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.Cancel,QMessageBox.StandardButton.Cancel)==QMessageBox.StandardButton.Yes


class Manager(QObject):
    def __init__(self,host,layout):
        super().__init__(host);self.host=host;self.folder=host.work_repository.path.parent
        self.repo=sr.Repository(host.work_repository.path);self.lock=sr.SchedulerLock(self.folder)
        self.owner=self.lock.acquire();self.worker=None;self.reader=None;self.publisher=None;self.cancel=None;self.running_id=None;self.closing=False
        self.dialog=None;self.status=label('定时补货：尚未设置','muted');self.schedules=[];self.recent=[]
        row=QHBoxLayout();row.addWidget(self.status,1);row.addWidget(button('定时补货',self.open));layout.insertLayout(2,row)
        self.host.import_page.layout().insertWidget(1,button('定时补货 · 设置与运行记录',self.open))
        from catalog_session_ui import manager
        self.catalog_owner=manager(host).session
        self.timer=QTimer(self);self.timer.setInterval(2000);self.timer.timeout.connect(self.poll)
        host.destroyed.connect(self.shutdown)
        if self.owner:self.repo.recover()
        self.timer.start();QTimer.singleShot(0,self.poll)

    def platform(self,cancel):return Platforms(self.folder,self.repo.path,self.host.aoxiang_workflows,self.catalog_owner,cancel)

    def busy(self):
        h=self.host
        pending=getattr(h,'_large_io_dialog',None)
        maintenance=getattr(getattr(h,'aoxiang_workflows',None),'maintenance_depth',0)
        return bool(getattr(h,'worker',None) or pending is not None and not pending._completed or
                    maintenance or
                    getattr(getattr(h,'browser_startup',None),'workers',None) or
                    getattr(h,'_aoxiang_dialog',None) or h.catalog_sessions.session.lease or
                    h.jst_generator.worker or getattr(getattr(h,'stockout',None),'worker',None) or getattr(h,'closing',False))

    def poll(self):
        if self.reader or self.closing:return
        pending=getattr(self.host,'_large_io_dialog',None)
        manager=getattr(self.host,'aoxiang_workflows',None)
        if (pending is not None and not pending._completed) or getattr(manager,'maintenance_depth',0):
            self.status.setText('定时检查等待当前保存、签发或数据维护结束；不会启动新作业');return
        def read(_):
            if not self.repo.exists():return None
            if self.owner:self.repo.due()
            return self.repo.schedules(),self.repo.list(limit=50)['items'],self.repo.next_queued()
        task=Call(read,self);self.reader=task
        def done(value):
            if value is None:return
            self.schedules,self.recent,queued=value
            self.queued_for_execution=bool(queued)
            stockout=getattr(self.host,'stockout',None)
            if queued and stockout and stockout.worker:stockout.pause('定时补货已排队，分析安全让行；可稍后继续原分析任务')
            active=next((r for r in self.recent if r['state'] in ('RUNNING','PAUSING','REVIEW','COMPLETE') and not r['viewed']),None)
            future=[s['next_at'] for s in self.schedules if s['enabled'] and s.get('next_at')]
            self.status.setText(('另一个窗口负责定时执行 · ' if not self.owner else '')+
                (active['schedule']['name']+' · '+sr.STATES[active['state']] if active else '下次：'+min(future)[5:16].replace('T',' ')+'（北京时间）' if future else '定时补货：未启用安排'))
            if self.owner and not self.worker and not self.publisher and not self.busy():
                pending=next((r for r in reversed(self.recent) if r['state']=='COMPLETE' and not r.get('publication')),None)
                if pending and not self.host.loaded and not self.host.job_dirty:self.publish(pending)
                elif queued:self.start(queued)
        def failed(error):
            if 'locked' in str(error).lower() or 'busy' in str(error).lower():
                self.status.setText('定时检查等待数据库释放，稍后自动重查；未启动新作业')
            else:self.status.setText('定时任务检查未完成：'+error)
        task.succeeded.connect(done);task.failed.connect(failed)
        task.finished.connect(lambda:self.reader_done(task));task.start()

    def reader_done(self,task):self.reader=None;task.deleteLater()

    def start(self,run):
        if self.worker or self.publisher or self.closing:return
        overlap=set((self.host.loaded or {}).get('stores',{})).intersection(s['code'] for s in run['schedule'].get('stores',[]))
        if overlap:
            self.repo.change(run['id'],state='REVIEW',message='当前工作台包含相同门店，请先保存并新建空白草稿，再继续原任务');return
        self.cancel=threading.Event();self.running_id=run['id']
        backend=self.platform(self.cancel)
        task=Call(lambda p:Runner(self.repo,backend,self.folder,self.cancel,p).execute(run['id']),self);self.worker=task
        task.progress.connect(self.status.setText);task.failed.connect(lambda e:self.status.setText('定时任务未完成：'+e))
        def finished():
            self.worker=None;self.cancel=None;self.running_id=None;task.deleteLater()
            if self.closing:QTimer.singleShot(0,self.host.close)
            else:self.poll()
        task.finished.connect(finished);task.start()

    def publish(self,run):
        task=Call(lambda _:publish_catalogs(self.repo,run['id'],self.folder),self);self.publisher=task
        def adopted(value):
            self.status.setText(value[0])
            if not self.host.loaded:self.host.catalogs=value[1];self.host.refresh_catalog_labels()
        def failed(error):
            prior=self.repo.get(run['id']).get('publication',{})
            self.repo.change(run['id'],publication={**prior,'state':'NEEDS_REVIEW'},message='计算已完成；默认仓资料未更新：'+error)
            self.status.setText('默认仓资料更新需核对；本次计算结果保持')
        task.succeeded.connect(adopted);task.failed.connect(failed)
        def finished():self.publisher=None;task.deleteLater()
        task.finished.connect(finished);task.start()

    def pause(self,rid,end=False):
        self.repo.request_pause(rid,end)
        if rid==self.running_id and self.cancel:self.cancel.set()

    def open(self):
        if self.dialog:self.dialog.raise_();self.dialog.activateWindow();return
        self.dialog=Window(self);self.dialog.setModal(True)
        self.dialog.finished.connect(lambda _:setattr(self,'dialog',None));self.dialog.show()
        if any(r['state'] in ('COMPLETE','REVIEW','PAUSED','RUNNING') for r in self.recent):self.dialog.tabs.setCurrentIndex(1)

    def prepare_close(self):
        if not self.closing and self.worker:
            if not ask(self.host,'退出前安全暂停','定时任务正在执行。暂停后退出？正在计算的门店会完成并保存；平台提交不会被强制中断。'):return False
        self.closing=True;self.timer.stop()
        if self.dialog and self.dialog.editor and self.dialog.editor.worker:
            self.dialog.editor.cancel.set();self.status.setText('等待连接核验安全结束后退出');QTimer.singleShot(500,self.host.close);return False
        if self.worker:
            if self.running_id:self.pause(self.running_id)
            self.status.setText('正在安全暂停定时任务，保留成功结果');return False
        if self.reader or self.publisher:QTimer.singleShot(200,self.host.close);return False
        return True

    def resume_after_cancelled_close(self):self.closing=False;self.timer.start()
    def shutdown(self):
        self.closing=True;self.timer.stop()
        if self.cancel:self.cancel.set()
        # Parent destruction is a final safety net, not a process termination.
        if self.reader:self.reader.wait()
        if self.worker:self.worker.wait()
        if self.publisher:self.publisher.wait()
        self.lock.close()


class Editor(QDialog):
    def __init__(self,parent,manager,existing=None):
        super().__init__(parent);self.m=manager;self.existing=existing;self.worker=None;self.cancel=threading.Event();self.verified=None
        self.setWindowTitle('设置定时补货');self.resize(720,600);v=QVBoxLayout(self)
        v.addWidget(label('选预案、定时间，上班后直接核对签发','title'))
        v.addWidget(label('电脑需开机且不休眠，软件保持打开；浏览器登录或授权失效时会停在“需处理”。','muted'))
        form=QFormLayout();v.addLayout(form);self.name=QLineEdit();form.addRow('安排名称',self.name)
        import replenishment_plans
        self.plans=replenishment_plans.Repository(manager.repo.path).list();self.plan=QComboBox()
        for p in self.plans:self.plan.addItem(p['name']+' · '+str(len(p['stores']))+'家',p['id'])
        form.addRow('补货预案',self.plan)
        self.clock=QTimeEdit();self.clock.setDisplayFormat('HH:mm');self.clock.setTime(self.clock.time().fromString('06:30','HH:mm'));form.addRow('开始时间（北京时间）',self.clock)
        self.kind=QComboBox();self.kind.addItem('每周重复','weekly');self.kind.addItem('仅指定日期','once');form.addRow('重复方式',self.kind)
        days=QHBoxLayout();self.days=[]
        for i,n in enumerate('一二三四五六日'):
            c=QCheckBox('周'+n);c.setChecked(i<5);days.addWidget(c);self.days.append(c)
        form.addRow('执行星期',days);self.date=QDateEdit();self.date.setCalendarPopup(True);self.date.setDate(self.date.date().currentDate());form.addRow('指定日期',self.date)
        self.workers=QSpinBox();self.workers.setRange(1,4);self.workers.setValue(2);form.addRow('同时计算门店数',self.workers)
        self.note=label('只获取、检查、计算和保存任务结果；不签发、不采购。','muted');v.addWidget(self.note)
        self.progress=QProgressBar();self.progress.hide();v.addWidget(self.progress)
        self.check=button('检查运行条件',self.verify,True);v.addWidget(self.check)
        self.stop=button('停止本次核验',self.cancel.set);self.stop.setEnabled(False);v.addWidget(self.stop)
        row=QHBoxLayout();self.enable=button('保存并启用',lambda:self.save(True),True);self.save_only=button('仅保存，不启用',lambda:self.save(False));row.addWidget(self.save_only);row.addWidget(self.enable);v.addLayout(row)
        if existing:
            self.name.setText(existing['name']);self.plan.setCurrentIndex(self.plan.findData(existing['plan_id']));self.clock.setTime(self.clock.time().fromString(existing['time'],'HH:mm'));self.kind.setCurrentIndex(self.kind.findData(existing['kind']))
            self.date.setDate(self.date.date().fromString(existing.get('date',sr.stamp().date().isoformat()),'yyyy-MM-dd'));self.workers.setValue(existing['workers'])
            for i,c in enumerate(self.days):c.setChecked(i in existing.get('weekdays',[]))
        else:self.name.setText(self.plan.currentText().split(' · ')[0] or '补货安排')
        self.kind.currentIndexChanged.connect(self.kind_changed);self.kind_changed()

    def kind_changed(self):
        once=self.kind.currentData()=='once';self.date.setEnabled(once)
        for d in self.days:d.setEnabled(not once)

    def value(self):
        plan=next((p for p in self.plans if p['id']==self.plan.currentData()),None)
        return sr.schedule_value({'name':self.name.text(),'plan_id':self.plan.currentData(),'plan_name':plan['name'] if plan else '', 'stores':plan['stores'] if plan else [],
            'time':self.clock.time().toString('HH:mm'),'kind':self.kind.currentData(),'date':self.date.date().toString('yyyy-MM-dd'),
            'weekdays':[i for i,c in enumerate(self.days) if c.isChecked()],'workers':self.workers.value(),'enabled':False})

    def verify(self):
        if self.worker:return
        try:value=self.value()
        except Exception as e:QMessageBox.warning(self,'无法核验',str(e));return
        self.cancel=threading.Event();self.verified=None;self.progress.setRange(0,0);self.progress.show();self.stop.setEnabled(True)
        for b in (self.check,self.enable,self.save_only):b.setEnabled(False)
        def action(p):
            basis=freeze(self.m.folder,value,sr.stamp().date().isoformat());sig=check_signature(basis)
            result=self.m.platform(self.cancel).preflight(basis,p)
            if check_signature(freeze(self.m.folder,value,basis['day']))!=sig:raise ValueError('核验期间参数已变化，请重新检查')
            return sig,result['message']
        task=Call(action,self);self.worker=task;task.progress.connect(self.note.setText)
        task.succeeded.connect(lambda result:self.verified_ok(value,result));task.failed.connect(self.note.setText)
        def finish():
            self.worker=None;task.deleteLater();self.progress.setRange(0,1);self.progress.setValue(1);self.stop.setEnabled(False)
            for b in (self.check,self.enable,self.save_only):b.setEnabled(True)
        task.finished.connect(finish);task.start()

    def verified_ok(self,value,result):self.verified=(value,result[0]);self.note.setText(result[1])

    def save(self,enabled):
        try:
            value=self.value();value['enabled']=enabled
            if enabled:
                if not self.verified or {**value,'enabled':False}!=self.verified[0]:raise ValueError('设置变化，请先检查运行条件；也可以仅保存')
                value['check_signature']=self.verified[1]
            self.m.repo.save_schedule(value,self.existing);self.accept();self.m.poll()
        except Exception as e:QMessageBox.warning(self,'未保存',str(e))

    def closeEvent(self,e):
        if self.worker:self.cancel.set();self.note.setText('正在安全停止核验，请等待');e.ignore()
        else:super().closeEvent(e)


class Window(QDialog):
    def __init__(self,manager):
        super().__init__(manager.host);self.m=manager;self.worker=None;self.editor=None;self.offset=0;self.current=None;self.records=[]
        self.setWindowTitle('定时补货');self.resize(1150,780);v=QVBoxLayout(self)
        v.addWidget(label('定时准备，人工签发','title'));v.addWidget(label('电脑和软件保持运行。错过时间不自动补跑；有问题只重试失败项，成功结果保留。','muted'))
        self.tabs=QTabWidget();v.addWidget(self.tabs);a=QWidget();b=QWidget();self.tabs.addTab(a,'执行安排');self.tabs.addTab(b,'运行记录')
        av=QVBoxLayout(a);actions=QHBoxLayout();av.addLayout(actions)
        for text,fn in [('新增安排',lambda:self.edit()),('编辑所选安排',self.edit_selected),('暂停未来执行',self.disable),('今天立即执行一次',self.manual),('删除执行安排',self.delete_schedule)]:actions.addWidget(button(text,fn))
        self.arrangements=table(['安排','补货预案','重复 / 开始时间','下次执行','状态']);av.addWidget(self.arrangements)
        av.addWidget(label('预案门店和默认参数在每次任务开始时冻结；修改后需重新检查运行条件。关闭此窗口不停止任务。','muted'))
        bv=QVBoxLayout(b);filters=QHBoxLayout();bv.addLayout(filters);self.query=QLineEdit();self.query.setPlaceholderText('搜索安排或门店');filters.addWidget(self.query)
        self.group=QComboBox()
        for text,data in [('全部记录','all'),('运行 / 暂停','active'),('需处理 / 错过','review'),('有可核对结果','ready'),('已结束','ended')]:self.group.addItem(text,data)
        filters.addWidget(self.group);filters.addWidget(button('查询',self.search));self.query.returnPressed.connect(self.search)
        self.runs=table(['日期 / 安排','状态','当前阶段','完成门店','最新情况']);self.runs.itemSelectionChanged.connect(self.select);bv.addWidget(self.runs,1)
        paging=QHBoxLayout();paging.addWidget(button('上一页',lambda:self.turn(-1)));paging.addWidget(button('下一页',lambda:self.turn(1)));self.page_note=label('');paging.addWidget(self.page_note);bv.addLayout(paging)
        self.detail=label('选择一次任务，查看门店进度。','muted');self.detail.setWordWrap(True);bv.addWidget(self.detail)
        self.bar=QProgressBar();bv.addWidget(self.bar);self.stores=table(['门店','处理状态','金额','说明']);bv.addWidget(self.stores,1)
        actions=QHBoxLayout();bv.addLayout(actions)
        self.run_actions={}
        for text,fn in [('核对已完成门店',self.open_result),('继续原任务',self.resume),('重试所选失败门店',self.retry),('暂停本次',lambda:self.pause(False)),('结束本次',lambda:self.pause(True)),('删除已结束记录',self.delete)]:
            b=button(text,fn,text=='核对已完成门店');self.run_actions[text]=b;actions.addWidget(b)
        details=QHBoxLayout();details.addWidget(button('查看所选门店当时参数',self.parameters));details.addWidget(button('采用本任务仓资料为默认',self.publish));bv.addLayout(details)
        self.note=label('','muted');v.addWidget(self.note);bottom=QHBoxLayout();bottom.addWidget(button('刷新',self.refresh));bottom.addWidget(button('关闭',self.close));v.addLayout(bottom)
        self.timer=QTimer(self);self.timer.setInterval(2000);self.timer.timeout.connect(self.refresh);self.timer.start();self.refresh()

    def call(self,fn,success=lambda _:None):
        if self.worker:return
        task=Call(fn,self);self.worker=task;task.succeeded.connect(success);task.failed.connect(self.note.setText)
        def done():self.worker=None;task.deleteLater()
        task.finished.connect(done);task.start()

    def refresh(self):
        if self.editor:return
        selected=self.current
        ai=self.arrangements.currentRow();store_code=None
        if self.stores.currentRow()>=0:
            try:store_code=self.selected()['stores'][self.stores.currentRow()]['code']
            except (ValueError,IndexError):pass
        # Capture widget values on GUI thread, never read widgets in a background worker.
        q=self.query.text().strip();g=self.group.currentData();offset=self.offset
        def read(_):return self.m.repo.schedules(),self.m.repo.list(offset=offset,limit=50,query=q,group=g)
        def show(value):
            self.ss,page=value;self.records=page['items'];self.total=page['total']
            fill(self.arrangements,[[s['name'],s.get('plan_name') or '预案待核对',('每周 '+''.join('一二三四五六日'[i] for i in s.get('weekdays',[])) if s['kind']=='weekly' else s['date'])+' '+s['time'],(s.get('next_at') or '—')[:16].replace('T',' '),'已启用' if s['enabled'] else '未启用'] for s in self.ss])
            if ai>=0 and ai<len(self.ss):self.arrangements.selectRow(ai)
            self.runs.blockSignals(True);fill(self.runs,[[r['day']+' / '+r['schedule']['name'],sr.STATES[r['state']],r['stage'],f"{sum(s['state']=='READY' for s in r['stores'])}/{len(r['stores'])}",r['message']] for r in self.records]);self.runs.blockSignals(False)
            self.page_note.setText(f'共{self.total}次 · 每页50次 · 当前第{self.offset//50+1}页')
            index=next((i for i,r in enumerate(self.records) if r['id']==selected),0 if self.records else -1)
            if index>=0:self.runs.selectRow(index);self.select()
            if store_code and index>=0:
                si=next((i for i,s in enumerate(self.records[index]['stores']) if s['code']==store_code),-1)
                if si>=0:self.stores.selectRow(si)
        self.call(read,show)

    def search(self):self.offset=0;self.refresh()
    def turn(self,n):self.offset=max(0,min(max(0,(getattr(self,'total',0)-1)//50*50),self.offset+n*50));self.refresh()
    def selected(self):
        i=self.runs.currentRow()
        if i<0 or i>=len(self.records):raise ValueError('请先选择任务')
        return self.records[i]
    def schedule(self):
        i=self.arrangements.currentRow()
        if i<0 or i>=len(getattr(self,'ss',[])):raise ValueError('请先选择执行安排')
        return self.ss[i]
    def select(self):
        try:r=self.selected()
        except ValueError:return
        self.current=r['id'];done=sum(s['state']=='READY' for s in r['stores'])
        active=r['state'] in ('RUNNING','PAUSING');counted=r['stage'] in ('计算门店','等待人工核对','已安全停止','需要处理')
        self.bar.setRange(0,(len(r['stores']) or 1) if counted or not active else 0);self.bar.setValue(done)
        elapsed=max(0,int((sr.stamp()-datetime.fromisoformat(r['created'])).total_seconds()))
        self.detail.setText(f"{r['stage']} · {'本次已用 '+str(elapsed//60)+'分'+str(elapsed%60)+'秒 · ' if active else ''}最后业务更新 {r['updated'][11:19]} · {r['message']}\n关闭窗口不会停止任务；停止需等运行中的门店安全完成。")
        allowed={'核对已完成门店':bool(done) and not active,'继续原任务':r['state'] in ('REVIEW','PAUSED'),'重试所选失败门店':r['state'] in ('REVIEW','PAUSED'),'暂停本次':r['state'] in ('RUNNING','QUEUED'),'结束本次':r['state'] not in ('ENDED','MISSED'),'删除已结束记录':r['state'] in ('ENDED','MISSED')}
        for name,b in self.run_actions.items():b.setEnabled(allowed[name])
        labels={'PENDING':'等待','READY':'可核对','RUNNING':'计算中','FAILED':'需处理'}
        fill(self.stores,[[s['name'],labels.get(s['state'],s['state']),f"{s['amount']:,.2f}" if 'amount'in s else '—',s['message']] for s in r['stores']])
    def edit(self,existing=None):
        if self.editor:return
        self.editor=Editor(self,self.m,existing);self.editor.finished.connect(lambda _:self.editor_done());self.editor.setModal(True);self.editor.show()
    def editor_done(self):self.editor=None;self.refresh()
    def guard(self,fn):
        try:fn()
        except Exception as e:QMessageBox.warning(self,'操作未完成',str(e))
    def edit_selected(self):self.guard(lambda:self.edit(self.schedule()))
    def disable(self):
        def act():
            s=self.schedule();self.m.repo.save_schedule({**s,'enabled':False},s);self.refresh()
        self.guard(act)
    def delete_schedule(self):
        def act():
            s=self.schedule()
            if ask(self,'删除执行安排',s['name']+'：取消未来触发，已经开始的任务和历史结果保留。'):
                self.m.repo.delete_schedule(s);self.refresh()
        self.guard(act)
    def manual(self):
        def act():
            s=self.schedule()
            if not s.get('check_signature'):raise ValueError('请编辑安排并检查运行条件，再执行')
            if ask(self,'今天执行一次','按该预案获取新文件并计算；不签发、不覆盖当前工作台。继续？'):
                self.m.repo.manual(s);self.tabs.setCurrentIndex(1);self.refresh();self.m.poll()
        self.guard(act)
    def resume(self):self.guard(lambda:self.m.repo.resume(self.selected()['id'],self.selected()['revision']));self.refresh()
    def retry(self):
        def act():
            r=self.selected();i=self.stores.currentRow()
            if i<0:raise ValueError('请选择一个未完成门店')
            self.m.repo.resume(r['id'],r['revision'],[r['stores'][i]['code']])
        self.guard(act);self.refresh()
    def pause(self,end):
        def act():
            r=self.selected()
            if end and not ask(self,'结束本次任务','保留已完成文件和结果，停止剩余步骤。不会删除草稿或签发；平台未决提交仍需核对。'):return
            self.m.pause(r['id'],end)
        self.guard(act);self.refresh()
    def delete(self):
        def act():
            r=self.selected()
            if ask(self,'删除本次记录',r['day']+' '+r['schedule']['name']+'：删除任务及其独立结果引用；已保存草稿、原文件和签发保持。'):
                self.m.repo.delete_run(r['id'],r['revision'])
        self.guard(act);self.refresh()
    def open_result(self):
        def act():
            r=self.selected();h=self.m.host
            if self.m.busy():raise ValueError('工作台有正在执行的操作，请结束后核对')
            if not h.leave_current():return
            def adopt(value):
                record,basis=value;h.restore_job(record,owned=True)
                h._calculation_profile=copy.deepcopy(basis['profile']);h._calculation_input=h.loaded;h._calculation_settings=copy.deepcopy(basis['jobs'])
                h.show_section(1);h.navigate(3);h.job_dirty=True
                self.m.repo.change(r['id'],viewed=True);self.accept()
            self.call(lambda _:workbench_record(self.m.repo,r['id']),adopt)
        self.guard(act)
    def parameters(self):
        def act():
            r=self.selected();i=self.stores.currentRow()
            if i<0:raise ValueError('请选择门店')
            code=r['stores'][i]['code']
            def display(basis):
                if not basis:raise ValueError('尚未冻结参数')
                from automation_jobs import parameters
                job=basis['jobs'][code];rows=[['预算','不限预算' if job['budget'] is None else str(job['budget'])+'元','门店已保存设置'],['主仓',job['supply_route']['primary'],'门店已保存设置'],['从仓',job['supply_route']['secondary'] or '无','门店已保存设置'],['必卖清单',basis['profile']['name'],'任务开始时冻结']]
                rows.extend([x['parameter'],str(x['value'])+' '+x['unit'],x['source']] for x in parameters(basis['records'][code]))
                d=QDialog(self);d.setWindowTitle(r['stores'][i]['name']+' · 当时生效参数');d.resize(850,650);v=QVBoxLayout(d);t=table(['业务参数','当时值','来源']);fill(t,rows);v.addWidget(t);v.addWidget(button('关闭',d.accept));d.exec()
            self.call(lambda _:self.m.repo.value(r['id'],'basis'),display)
        self.guard(act)
    def publish(self):
        def act():
            r=self.selected()
            if self.m.host.loaded or self.m.busy():raise ValueError('当前工作台已载入数据或正在操作；请先保存并关闭当前草稿，默认资料保持不动')
            if not ask(self,'更新默认仓资料','仅采用本任务已验证的仓资料，供以后检查文件使用；不替换历史、当前草稿或聚水潭分配库存。采集后默认已被修改的仓会拒绝覆盖。'):return
            def apply(p):return publish_catalogs(self.m.repo,r['id'],self.m.folder)
            def adopted(result):
                self.note.setText(result[0]);self.m.host.catalogs=result[1];self.m.host.refresh_catalog_labels()
            self.call(apply,adopted)
        self.guard(act)
    def closeEvent(self,e):
        if self.worker or self.editor and self.editor.worker:self.note.setText('请等待当前读取或核验安全结束');e.ignore()
        else:self.timer.stop();super().closeEvent(e)
