"""Optional module shell. No analysis DB, heavy imports or worker on ordinary startup."""
from datetime import datetime
from PySide6.QtCore import QObject,QTimer
from ui_controls import button


class Entry(QObject):
    nav_text='门店缺货率'
    page_module='stockout_ui'
    trigger_module='stockout_trigger'
    def __init__(self,host,*,navigation=True,schedule=True):
        super().__init__(host)
        self.host=host;self.page=None;self.index=None;self.generation=0
        self.worker=None;self.callback=None;self.queued=None;self.watch=None;self.timer=None
        self.closing=False;self.background=False;self.message='按需进入；每日采集默认关闭'
        self.nav=button(self.nav_text,self.enter)
        if navigation:host.centralWidget().layout().itemAt(0).layout().insertWidget(5,self.nav)
        else:self.nav.hide()
        host.sections.currentChanged.connect(self.section_changed)
        if schedule:self.arm()

    def arm(self):
        if self.timer:self.timer.stop();self.timer.deleteLater();self.timer=None
        if self.closing:return
        try:
            load=self.trigger().load
            from stockout_common import TZ
            plan=load(self.host.repository.folder)
            if not plan['enabled']:return
            delay=max(1,int((datetime.fromisoformat(plan['next_at'])-datetime.now(TZ)).total_seconds()*1000))
            self.timer=QTimer(self);self.timer.setSingleShot(True);self.timer.timeout.connect(self.due)
            self.timer.start(min(delay,2147483647))
        except Exception as exc:self.status(str(exc))

    def due(self):
        if self.closing:return
        try:
            due=self.trigger().due
            value=due(self.host.repository.folder)
            if value and value.get('missed'):self.status(value['message'])
            elif value:
                if not self.worker and not self.queued:self.start('collect',value,background=True)
                else:self.status('每日采集时点已有分析任务；不重复启动，可在板块核对当日覆盖')
        except Exception as exc:self.status('每日分析未启动：'+str(exc))
        self.arm()

    def enter(self):
        if self.closing:return
        if self.page is None:
            self.generation+=1;self.page=self.make_page()
            self.index=self.host.sections.addWidget(self.page)
        self.host.show_section(self.index)
        self.page.refresh_metadata()

    def section_changed(self,index):
        if self.page is None or index==self.index:return
        page=self.page;self.page=None;self.index=None;self.generation+=1;self.callback=None
        if self.queued and not self.queued[3]:self.queued=None
        if self.worker and not self.background:
            self.pause('已离开分析，正在安全暂停并释放资源')
        self.host.sections.removeWidget(page);page.deleteLater()

    def status(self,message):
        import logging
        logging.debug('缺货分析状态：%s',message)
        self.message=message;self.nav.setToolTip(message)
        if self.page is not None:self.page.note.setText(message)

    def busy(self):
        h=self.host;scheduled=getattr(h,'scheduled',None);ax=getattr(h,'aoxiang_workflows',None)
        io=getattr(h,'_large_io_dialog',None);generator=getattr(h,'jst_generator',None)
        other=any(e is not self and getattr(e,'worker',None) for e in (getattr(h,'stockout',None),getattr(h,'warehouse_stockout',None)) if e is not None)
        return bool(other or getattr(h,'worker',None) or getattr(h,'closing',False)
                    or io is not None and not getattr(io,'_completed',False)
                    or getattr(h,'_aoxiang_dialog',None)
                    or scheduled and (scheduled.worker or scheduled.publisher or getattr(scheduled,'queued_for_execution',False))
                    or ax and (ax.worker or ax.maintenance_depth or ax.gate.foreground is not None)
                    or generator and generator.worker and getattr(generator,'active_work',True))

    def start(self,operation,args=None,callback=None,*,background=False):
        if self.closing:return
        request=(operation,args or {},callback,background,self.generation)
        if self.worker or self.busy():
            if self.worker and self.worker.operation not in {'collect','adopt','end','cleanup','ingest','cleanup_retry','delete_run_log','rerun','recalculate','save_views','hub_collect','hub_scheduled','hub_end','hub_reuse','hub_export'}:
                self.callback=None;self.worker.cancel_reason='已按新请求取消上次查询';self.worker.cancel.set()
            self.queued=request;self.status('等待当前任务安全结束；原补货业务优先')
            self.ensure_watch();return
        self.begin(request)

    def ensure_watch(self):
        if self.watch is None:
            self.watch=QTimer(self);self.watch.setInterval(300);self.watch.timeout.connect(self.tick)
        self.watch.start()

    def begin(self,request):
        operation,args,callback,background,generation=request
        self.background=background;self.callback=(generation,callback)
        task=self.make_task(operation,args);self.worker=task
        task.progress.connect(self.status)
        task.succeeded.connect(self.success)
        task.failed.connect(self.failure)
        task.finished.connect(self.finished)
        self.status('正在处理分析请求…');self.ensure_watch();task.start()
        if self.page:self.page.controls()

    def make_task(self,operation,args):
        from stockout_task import Task
        return Task(self.host,operation,args)

    def trigger(self):
        import stockout_trigger
        return stockout_trigger

    def make_page(self):
        from stockout_ui import Page
        return Page(self.host,self)

    def tick(self):
        if self.worker:
            if self.busy() and not self.worker.cancel.is_set():self.pause('原补货业务优先，分析正在安全让行；之后可继续原任务')
        elif self.queued and not self.busy() and not self.closing:
            request=self.queued;self.queued=None
            if request[1].get('trigger','').startswith('schedule:'):
                from stockout_common import today
                if request[1]['trigger']!='schedule:'+today():
                    self.status('原分析计划已跨日错过，未补写昨日库存；可手动采集今日');return
            if request[3] or request[4]==self.generation and self.page is not None:self.begin(request)
        elif not self.queued and self.watch:
            self.watch.stop();self.watch.deleteLater();self.watch=None

    def success(self,result):
        self.status('分析操作完成；资料仅保存在本板块')
        callback=self.callback;self.callback=None
        if callback and callback[0]==self.generation and self.page is not None and callback[1]:callback[1](result)

    def failure(self,message):
        self.status('分析未完成：'+message+'；原补货资料保持不变')
        if self.page:self.page.last_error=message

    def finished(self):
        task=self.worker;self.worker=None;self.callback=None;self.background=False
        if task:task.deleteLater()
        if self.page:self.page.controls()
        if self.closing:QTimer.singleShot(0,self.host.close)
        else:
            if self.page and task and task.operation in {'collect','rerun','recalculate'} and not self.queued:self.page.refresh_metadata()
            self.tick()

    def pause(self,message='已请求安全暂停；原件和原任务编号保留'):
        if self.worker:self.worker.cancel_reason=message;self.worker.cancel.set()
        self.queued=None;self.status(message)

    def prepare_close(self):
        self.closing=True;self.queued=None
        if self.timer:self.timer.stop()
        if self.worker:
            self.pause('等待分析安全结束后退出，不强杀提交任务');return False
        if self.watch:self.watch.stop()
        return True

    def resume_after_cancelled_close(self):
        self.closing=False;self.arm()

    def set_schedule(self,enabled,at):
        save=self.trigger().save
        save(self.host.repository.folder,enabled,at)
        if not enabled and self.queued and self.queued[1].get('trigger','').startswith('schedule:'):self.queued=None
        self.arm()
        self.status('已启用每日采集；到时会后台取数，完成后释放' if enabled else '已停用未来每日采集；当前任务可单独安全暂停')


def install(host):host.stockout=Entry(host)
