"""Run an extracted EXE with a private fixture and sample its process tree.

The operator drives the application dialogs, recording start.json immediately
before the final confirmation and result.json when the result appears. No test
flag, import, platform connection or application-code replacement is used.
"""
import argparse
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

from export_files import sha256


class ProcessEntry(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('usage', wintypes.DWORD), ('pid', wintypes.DWORD),
                ('heap', ctypes.c_size_t), ('module', wintypes.DWORD), ('threads', wintypes.DWORD),
                ('parent', wintypes.DWORD), ('priority', ctypes.c_long), ('flags', wintypes.DWORD),
                ('exe', wintypes.WCHAR * 260)]


class Memory(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('faults', wintypes.DWORD)] + [
        (field, ctypes.c_size_t) for field in ('peak', 'working', 'peak_paged', 'paged', 'peak_nonpaged',
                                             'nonpaged', 'pagefile', 'peak_pagefile')]


def working_set_tree(pid):
    kernel = ctypes.WinDLL('kernel32')
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.Process32FirstW.argtypes = kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError()
    children = {}
    try:
        row = ProcessEntry()
        row.size = ctypes.sizeof(row)
        more = kernel.Process32FirstW(snapshot, ctypes.byref(row))
        while more:
            children.setdefault(row.parent, []).append(row.pid)
            more = kernel.Process32NextW(snapshot, ctypes.byref(row))
    finally:
        kernel.CloseHandle(snapshot)
    tree = [pid]
    for parent in tree:
        tree.extend(children.get(parent, ()))
    values = {}
    get_memory = ctypes.WinDLL('psapi').GetProcessMemoryInfo
    get_memory.argtypes = [wintypes.HANDLE, ctypes.POINTER(Memory), wintypes.DWORD]
    for child in tree:
        handle = kernel.OpenProcess(0x1010, False, child)
        if handle:
            try:
                counters = Memory()
                counters.size = ctypes.sizeof(counters)
                if get_memory(handle, ctypes.byref(counters), counters.size):
                    values[str(child)] = counters.working
            finally:
                kernel.CloseHandle(handle)
    return values


def marker(file):
    try:
        return json.loads(file.read_text('utf-8'))
    except (FileNotFoundError, ValueError):
        return None


def run(exe, fixture, root, mode):
    details = marker(fixture/'fixture.json')
    if not details or not details['synthetic'] or details['live_platform']:
        raise ValueError('Isolated fixture required')
    root.mkdir()
    settings, output, temp = (root/name for name in ('settings', 'export', 'temp'))
    shutil.copytree(fixture/'settings', settings)
    actual = sha256(settings/'workbench.sqlite3')
    if actual != details['database_sha256']:
        raise ValueError('Baseline fixture digest changed')
    output.mkdir()
    temp.mkdir()
    (settings/'export_settings.json').write_text(json.dumps({'schema':1,'default_export_directory':str(output),
        'monthly_new_sheet_editable':False},ensure_ascii=False), 'utf-8')
    info = {'mode':mode,'exe':str(exe),'exe_sha256':sha256(exe),'input_sha256':actual,
            'build': marker(exe.parent/'版本信息.json')}
    env = {**os.environ,'REPLENISHMENT_DATA_DIR':str(settings),'LOCALAPPDATA':str(root/'local'),
           'APPDATA':str(root/'roaming'),'TEMP':str(temp),'TMP':str(temp),
           'REPLENISHMENT_IO_TRACE':str(root/'trace'),'PYTHONUTF8':'1'}
    for key in ('QT_QPA_PLATFORM','PYTHONPATH','PYTHONHOME','REPLENISHMENT_PORTABLE_PATHS'):
        env.pop(key,None)
    with (root/'process.log').open('wb') as log, (root/'working-set.jsonl').open('w',encoding='utf-8') as samples:
        process = subprocess.Popen([str(exe)], cwd=exe.parent, env=env, stdout=log, stderr=log,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        (root/'pid.txt').write_text(str(process.pid),'ascii')
        start = None
        result = None
        peak = 0
        at_peak = {}
        while process.poll() is None:
            start = start or marker(root/'start.json')
            if start:
                result = marker(root/'result.json')
                if result:
                    break
                tree = working_set_tree(process.pid)
                sample = sum(tree.values())
                if sample > peak:
                    peak, at_peak = sample, tree
                samples.write(json.dumps({'time':time.time(),'bytes':sample,'processes':tree})+'\n')
                samples.flush()
            time.sleep(.2)
        if not result:
            result = {'time':time.time(),'state':'EXITED','exit_code':process.returncode}
    files = [{'file':p.name,'bytes':p.stat().st_size,'sha256':sha256(p)} for p in output.glob('*.xlsx')]
    receipt = {**info,'start':start,'result':result,'seconds':result['time']-start['time'] if start else None,
               'peak_tree_bytes':peak,'peak_tree_processes':at_peak,'files':files,
               'output_bytes':sum(f['bytes'] for f in files)}
    (root/'timing.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2),'utf-8')
    print(json.dumps({k:receipt[k] for k in ('mode','seconds','result','peak_tree_bytes','output_bytes')},ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    for name in ('exe','fixture','out'):
        parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--mode',choices=('return','operations','signed'),required=True)
    arguments = parser.parse_args()
    run(arguments.exe.resolve(),arguments.fixture.resolve(),arguments.out.resolve(),arguments.mode)
