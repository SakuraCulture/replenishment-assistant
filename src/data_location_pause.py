"""Temporary automatic-work pause while the data-location maintenance panel is open."""
import time
from runtime_support import checkpoint

class AutomaticPause:
    def __init__(self,host):
        self.host=host;self.flags=[];self.timers=[];self.restored=False;self.started=time.monotonic()
        self.previous=getattr(host,'_data_location_preparing',False);host._data_location_preparing=True
        owners=[('browser_startup','paused'),('browser_status','paused'),('jst_generator','stopped'),
                ('aoxiang_workflows','stopping'),('scheduled','closing')]
        for name,flag in owners:
            owner=getattr(host,name,None)
            if owner is None:continue
            if hasattr(owner,flag):self.flags.append((owner,flag,getattr(owner,flag)));setattr(owner,flag,True)
            for attr in ('timer','initial'):
                timer=getattr(owner,attr,None)
                if timer is not None:self.timers.append((timer,timer.isActive()));timer.stop()
        host._data_location_pause=self
    def restore(self):
        if self.restored:return
        self.restored=True;self.host._data_location_preparing=self.previous
        for owner,attr,value in self.flags:setattr(owner,attr,value)
        startup=getattr(self.host,'browser_startup',None)
        if startup and getattr(startup,'retry_until',0)>0:startup.retry_until+=time.monotonic()-self.started
        for timer,active in self.timers:
            if active:timer.start()
            else:timer.stop()
        if getattr(self.host,'_data_location_pause',None) is self:self.host._data_location_pause=None

def finish_login_checks(workers,progress=lambda _:None,cancel=None):
    """Cancel only startup's read-only probes and wait for their existing leases."""
    for worker in workers:worker.cancel.set()
    for worker in workers:
        while not worker.released.wait(.1):
            checkpoint(cancel);progress('等待已启动的登录检查安全让出连接；未开始搬迁')
    checkpoint(cancel)
