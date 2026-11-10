"""Lightweight warehouse navigation. No facts or originals read at startup."""
from stockout_entry import Entry as BaseEntry

class Entry(BaseEntry):
    nav_text='大仓缺货率'
    page_module='warehouse_stockout_ui'
    trigger_module='warehouse_stockout_trigger'
    def trigger(self):
        import warehouse_stockout_trigger
        return warehouse_stockout_trigger
    def make_page(self):
        from warehouse_stockout_ui import Page
        return Page(self.host,self)
    def make_task(self,operation,args):
        from stockout_task import Task,Bridge
        from warehouse_stockout_worker import serve
        class WarehouseBridge(Bridge):
            AX_ACTIONS=set()
        return Task(self.host,operation,args,bridge_factory=WarehouseBridge,worker_target=serve)

def install(host):host.warehouse_stockout=Entry(host)
