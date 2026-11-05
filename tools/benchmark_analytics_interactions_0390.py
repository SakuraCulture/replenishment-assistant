"""Synthetic, sequential interaction timings; fresh output folder only.

Samples the Windows working set of this process and its current descendants.
No cache flushing, real orders, user configuration, or platform operations.
"""
import argparse
import ctypes as c
from ctypes import wintypes as w
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'src'), str(ROOT/'tests'), str(ROOT/'tools')]
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')


class Entry(c.Structure):
    _fields_ = [('size',w.DWORD),('usage',w.DWORD),('pid',w.DWORD),('heap',c.c_size_t),
                ('module',w.DWORD),('threads',w.DWORD),('parent',w.DWORD),('priority',w.LONG),
                ('flags',w.DWORD),('exe',w.WCHAR*260)]


class Memory(c.Structure):
    _fields_ = [('cb',w.DWORD),('faults',w.DWORD)] + [(x,c.c_size_t) for x in
        ('peak','working','quota_peak_paged','quota_paged','quota_peak_nonpaged','quota_nonpaged','pagefile','peak_pagefile')]


def memory_tree():
    kernel=c.WinDLL('kernel32',use_last_error=True);api=c.WinDLL('psapi',use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes=[w.DWORD,w.DWORD];kernel.CreateToolhelp32Snapshot.restype=w.HANDLE
    kernel.Process32FirstW.argtypes=[w.HANDLE,c.POINTER(Entry)];kernel.Process32NextW.argtypes=[w.HANDLE,c.POINTER(Entry)]
    kernel.CloseHandle.argtypes=[w.HANDLE];kernel.OpenProcess.argtypes=[w.DWORD,w.BOOL,w.DWORD];kernel.OpenProcess.restype=w.HANDLE
    api.GetProcessMemoryInfo.argtypes=[w.HANDLE,c.POINTER(Memory),w.DWORD]
    handle=kernel.CreateToolhelp32Snapshot(2,0)
    if handle==c.c_void_p(-1).value:raise c.WinError(c.get_last_error())
    entry=Entry();entry.size=c.sizeof(entry);parents={}
    try:
        valid=kernel.Process32FirstW(handle,c.byref(entry))
        while valid:
            parents[entry.pid]=entry.parent;valid=kernel.Process32NextW(handle,c.byref(entry))
    finally:kernel.CloseHandle(handle)
    selected={os.getpid()}
    while True:
        found={pid for pid,parent in parents.items() if parent in selected}
        if found<=selected:break
        selected.update(found)
    total=0;count=0
    for pid in selected:
        handle=kernel.OpenProcess(0x1000,False,pid)
        if not handle:continue  # A short-lived worker may already have exited.
        try:
            info=Memory();info.cb=c.sizeof(info)
            if api.GetProcessMemoryInfo(handle,c.byref(info),info.cb):total+=info.working;count+=1
        finally:kernel.CloseHandle(handle)
    return total,count


def measure(fn,app):
    from PySide6.QtCore import QTimer
    samples=[];errors=[];beats=[];stop=threading.Event()
    def sample():
        try:
            while not stop.is_set():samples.append(memory_tree());stop.wait(.03)
        except Exception as exc:errors.append(repr(exc))
    monitor=threading.Thread(target=sample);timer=QTimer();timer.setInterval(25);timer.timeout.connect(lambda:beats.append(time.perf_counter()))
    timer.start();monitor.start();start=time.perf_counter()
    try:value=fn();elapsed=time.perf_counter()-start
    finally:stop.set();monitor.join();timer.stop()
    if errors:raise RuntimeError(errors)
    return value,{'seconds':elapsed,'peak_process_tree_working_set_mb':max(v for v,_ in samples)/1024**2,
                  'max_sampled_processes':max(n for _,n in samples),'heartbeat_ticks':len(beats),
                  'maximum_heartbeat_gap_seconds':max((b-a for a,b in zip(beats,beats[1:])),default=None)}


def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);a=p.parse_args();out=a.output.resolve()
    out.mkdir(parents=True,exist_ok=False)
    from verify_analytics_0390 import fixture
    path,spec=fixture(out)
    os.environ['REPLENISHMENT_DATA_DIR']=str(out/'settings')
    from PySide6.QtWidgets import QApplication,QWidget
    from PySide6.QtTest import QTest
    from order_analytics_ui import AnalyticsWidget
    import history_diagnostics
    app=QApplication.instance() or QApplication([])
    host=QWidget();host.work_repository=SimpleNamespace(path=path);host.repository=SimpleNamespace(folder=out/'settings');host.closing=False
    widget=AnalyticsWidget(host);widget.update_timer.stop();runs={}
    def record(name,fn):
        value,metric=measure(fn,app);runs.setdefault(name,[]).append(metric);return value
    def wait():
        deadline=time.monotonic()+180
        while widget.worker and time.monotonic()<deadline:QTest.qWait(10)
        if widget.worker:raise TimeoutError('Query still running')
        if widget.report is None:raise AssertionError('Query failed')
    for i in range(3):
        # New interpreter/module caches each time. OS file cache is not flushed.
        script="import sys;sys.path.insert(0,sys.argv[1]);import order_analytics as a,json; r=a.collect(sys.argv[2],json.loads(sys.argv[3]));assert len(r['rows'])==6000"
        record('fresh_process_query_including_import',lambda:subprocess.run([sys.executable,'-B','-c',script,str(ROOT/'src'),str(path),json.dumps(spec)],check=True))
        widget.search.setText('');record('native_query_until_ui_ready',lambda:(widget.query(),wait()))
        record('same_report_history_projection',lambda:history_diagnostics.review(widget.report))
        def drill():
            widget.open_detail('store',0);widget.open_detail('warehouse',0);widget.open_detail('order',0);app.processEvents();widget.back()
        record('three_level_drill_and_return',drill)
        widget.search.setText('合成门店000');record('filtered_query_until_ui_ready',lambda:(widget.query(),wait()))
    widget.deleteLater();host.deleteLater();app.processEvents()
    import test_automation_gateway as fixtures
    from retry_profile_smoke import install
    fixtures.GatewayRegression.setUpClass();f=fixtures.GatewayRegression();f.setUp()
    try:
        install(f.win,'B');f.win.concurrency.setValue(2)
        for i in range(3):
            args={'operation_key':'perf-'+str(i),'context_token':f.g.token(),'stores':['S1','S2'],'categories':['快销']}
            def trial():
                task=f.g.start_stock_diagnosis(args);f.wait()
                if f.g.task_record(task['task_id'])['state']!='completed':raise AssertionError(f.g.task_record(task['task_id']))
                return task
            task=record('two_store_baseline_plus_two_candidates',trial)
            receipt=record('adopt_complete_candidate',lambda:f.g.adopt_stock_diagnosis({'task_id':task['task_id'],'context_token':f.g.token(),'candidate':1,'reason':'合成性能验收','accept_risks':True}))
            record('undo_candidate',lambda:f.g.undo_stock_diagnosis({'task_id':task['task_id'],'context_token':receipt['context_token']}))
    finally:f.tearDown()
    report={'synthetic':True,'scope':{'history_stores':12,'history_rows':6000,'trial_stores':2,'trial_skus_per_store':2},
            'limitations':'同机串行3次；新进程包含导入但未清理操作系统文件缓存，不等于冷盘。试算仅2店各2SKU，主要验证spawn与编排开销，不能外推大店吞吐。30ms抽样本进程及实时后代工作集相加，共享页可能重复计入，短命进程和瞬时峰值可能漏采；子进程含冷查询解释器/计算工作进程。Qt 25ms心跳仅在有事件处理的查询/试算内衡量响应；同步短操作无样本不代表零卡顿。',
            'runs':runs,'summary':{name:{'median_seconds':statistics.median(r['seconds'] for r in data),
                'min_seconds':min(r['seconds'] for r in data),'max_seconds':max(r['seconds'] for r in data),
                'peak_process_tree_working_set_mb':max(r['peak_process_tree_working_set_mb'] for r in data),
                'max_sampled_processes':max(r['max_sampled_processes'] for r in data)} for name,data in runs.items()}}
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8');print(json.dumps(report['summary'],ensure_ascii=False,indent=2))


if __name__=='__main__':main()
