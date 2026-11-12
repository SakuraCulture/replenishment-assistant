"""Small status IO; Windows readers must allow atomic replacement by the mover."""
import json,os,time
from pathlib import Path
from stockout_common import atomic_json

def read(path):
    if os.name=='nt':
        import ctypes,msvcrt
        from ctypes import wintypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
        kernel.CreateFileW.restype=wintypes.HANDLE
        handle=kernel.CreateFileW(str(Path(path).absolute()),0x80000000,7,None,3,0,None)
        if handle==ctypes.c_void_p(-1).value:raise ctypes.WinError(ctypes.get_last_error())
        try:fd=msvcrt.open_osfhandle(handle,os.O_RDONLY|os.O_BINARY)
        except BaseException:
            kernel.CloseHandle.argtypes=[wintypes.HANDLE];kernel.CloseHandle(handle);raise
        with os.fdopen(fd,'rb') as file:raw=file.read(1024*1024+1)
    else:
        with Path(path).open('rb') as file:raw=file.read(1024*1024+1)
    if len(raw)>1024*1024:raise ValueError('搬迁记录大小异常，请保留现场')
    return json.loads(raw.decode('utf-8'))

def save(path,value):
    # A legacy dialog/antivirus may momentarily have a non-delete-sharing
    # handle. Retry only this exact atomic status publication, never a move.
    for attempt in range(16):
        try:return atomic_json(path,value)
        except PermissionError as exc:
            if os.name!='nt' or getattr(exc,'winerror',None) not in (5,32,33) or attempt==15:raise
            time.sleep(.05)
