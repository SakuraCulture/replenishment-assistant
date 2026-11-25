"""Admission for optional overlapping browser and local work on one desktop."""
from pathlib import Path
import shutil
import threading
import time

from adaptive_resources import Budget
from performance_counters import Counters
from stockout_common import atomic_json, read_json

GIB=1024**3


def configured(folder):
    file=Path(folder)/'unattended-resources.json'
    try:return file.is_file() and read_json(file,8192).get('parallel') is True
    except (OSError,ValueError,AttributeError):return False


def configure(folder,enabled):
    atomic_json(Path(folder)/'unattended-resources.json',{'parallel':bool(enabled)})


class Resources:
    def __init__(self,folder,*,budget=None,system=None,clock=time.monotonic):
        self.folder=Path(folder);self.clock=clock;self.budget=budget
        self.system=system or Counters().system
        self.lock=threading.RLock();self.mode='normal';self.last_sample=-float('inf')
        self.high_cpu_since=None;self.recover_since=None;self.previous_cpu=None;self.cpu=None
        self.browser_limit=3;self.heavy_owner=None;self.reason='';self.events=[]

    def disk_available(self,folder):
        path=Path(folder)
        while not path.exists() and path.parent!=path:path=path.parent
        try:return shutil.disk_usage(path).free>=GIB
        except OSError:return False

    def check(self):
        with self.lock:
            if not configured(self.folder):return 'serial'
            now=self.clock()
            if now-self.last_sample<.5:return self.mode
            self.last_sample=now;self.budget=self.budget or Budget();self.budget.refresh()
            try:data=self.system()
            except (OSError,ValueError):data={}
            idle,total=data.get('cpu_idle_ticks'),data.get('cpu_total_ticks')
            if idle is not None and total is not None:
                if self.previous_cpu is not None and total>self.previous_cpu[1]:
                    self.cpu=100*(1-(idle-self.previous_cpu[0])/(total-self.previous_cpu[1]))
                self.previous_cpu=(idle,total)
            hot=self.cpu is not None and self.cpu>=85
            self.high_cpu_since=(self.high_cpu_since if self.high_cpu_since is not None else now) if hot else None
            head=self.budget.headroom
            tight_cpu=hot and now-self.high_cpu_since>=15
            prior=self.mode;reason=''
            if head is None or self.budget.mode=='unknown':self.mode='serial';reason='资源采样不足'
            elif head<GIB or self.budget.mode=='critical':self.mode='protected';reason='可用内存或提交余量不足'
            elif head<2*GIB or self.budget.mode=='tight' or tight_cpu:self.mode='tight';reason='资源紧张，暂缓重型任务'
            elif prior!='normal' or self.browser_limit<3:
                cool=head>=4*GIB and self.cpu is not None and self.cpu<65
                self.recover_since=(self.recover_since if self.recover_since is not None else now) if cool else None
                if self.recover_since is not None and now-self.recover_since>=(60 if prior!='normal' else 30):
                    if prior!='normal':self.mode='normal';self.browser_limit=1
                    else:self.browser_limit=min(3,self.browser_limit+1)
                    self.recover_since=now
                if self.mode!='normal' or self.browser_limit<3:reason='资源回升观察'
            elif head<4*GIB or self.cpu is None or self.cpu>=75:
                reason='资源裕量不足，暂不增加并发'
            if self.mode in ('serial','protected','tight'):
                self.browser_limit=1
                if reason!='资源回升观察':self.recover_since=None
            if reason!=self.reason or self.mode!=prior:
                self.reason=reason;self.events.append((now,self.mode,reason));self.events=self.events[-100:]
            return self.mode

    def warehouse_slots(self):
        mode=self.check()
        return 2 if mode=='normal' and self.browser_limit>=3 and not self.reason else 1

    def try_heavy(self,owner,folder):
        with self.lock:
            mode=self.check()
            if self.heavy_owner is not None or mode in ('tight','protected') or not self.disk_available(folder):return False
            if mode=='normal' and self.budget.headroom<2*GIB:return False
            self.heavy_owner=owner;return True

    def release_heavy(self,owner):
        with self.lock:
            if self.heavy_owner is owner:self.heavy_owner=None
