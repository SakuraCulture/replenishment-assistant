"""Single lazy navigation with independently loaded store/warehouse pages."""
from stockout_entry import Entry as BaseEntry


class Child(BaseEntry):
    def __init__(self,host,hub,kind):
        self.hub,self.kind=hub,kind
        super().__init__(host,navigation=False,schedule=False)
    def enter(self):self.hub.enter_kind(self.kind)
    def section_changed(self,index):pass  # The containing hub owns page disposal.
    def arm(self):self.hub.arm()
    def busy(self):return bool(self.hub.worker) or super().busy()
    def make_page(self):
        if self.kind=='warehouse':
            from warehouse_stockout_ui import Page
        else:
            from stockout_ui import Page
        return Page(self.host,self)
    def trigger(self):
        if self.kind=='warehouse':import warehouse_stockout_trigger as trigger
        else:import stockout_trigger as trigger
        return trigger
    def make_task(self,operation,args):
        if self.kind=='store':return super().make_task(operation,args)
        from stockout_task import Task,Bridge
        from warehouse_stockout_worker import serve
        class WarehouseBridge(Bridge):AX_ACTIONS=set()
        return Task(self.host,operation,args,bridge_factory=WarehouseBridge,worker_target=serve)
    def finished(self):
        super().finished()
        if self.hub.page:self.hub.page.controls()
    def begin(self,request):
        super().begin(request)
        if self.hub.page:self.hub.page.controls()
    def status(self,message):
        super().status(message)
        if self.hub.page:self.hub.page.note.setText(('门店：' if self.kind=='store' else '大仓：')+message)


class Entry(BaseEntry):
    nav_text='缺货率'
    def __init__(self,host):
        self.children={};super().__init__(host)
        self.children={'store':Child(host,self,'store'),'warehouse':Child(host,self,'warehouse')}
        host.stockout=self.children['store'];host.warehouse_stockout=self.children['warehouse']
    def make_page(self):
        from stockout_hub_ui import Page
        return Page(self.host,self)
    def make_task(self,operation,args):
        from stockout_task import Task
        from stockout_hub_worker import serve
        return Task(self.host,operation,args,worker_target=serve)
    def enter_kind(self,kind):
        self.initial_kind=kind
        self.enter()
        if self.page:self.page.tabs.setCurrentIndex(0 if kind=='store' else 1)
    def section_changed(self,index):
        if self.page is not None and index!=self.index:
            for child in self.children.values():
                if child.page:
                    if child.worker and not child.background:child.pause('离开缺货率，正在安全暂停')
                    child.page=None;child.index=None;child.generation+=1;child.callback=None
        super().section_changed(index)
    def finished(self):
        task=self.worker;super().finished()
        if self.page and task and task.operation in ('hub_collect','hub_scheduled','hub_end','hub_reuse'):self.page.refresh_metadata()
    def arm(self):
        from stockout_hub_schedule import arm
        arm(self)
    def due(self):
        from stockout_hub_schedule import due
        due(self)
    def tick(self):
        super().tick()
        from stockout_hub_schedule import drain
        drain(self)
    def pause(self,message='已请求安全暂停；原任务和原件保留'):
        super().pause(message)
        for child in self.children.values():
            if child.worker:child.pause(message)
    def prepare_close(self):
        ready=super().prepare_close()
        for child in self.children.values():ready=child.prepare_close() and ready
        return ready
    def resume_after_cancelled_close(self):
        for child in self.children.values():child.closing=False
        super().resume_after_cancelled_close()


def install(host):host.stockout_hub=Entry(host)
