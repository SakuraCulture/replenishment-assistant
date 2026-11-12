"""Create/remove one exact Windows junction, without a shell or elevated symlink privilege."""
import ctypes,os,struct
from pathlib import Path

def create(source,target):
    source=Path(source).absolute();target=Path(target).resolve(strict=True)
    if os.name!='nt':raise OSError('资料位置兼容链接仅支持Windows')
    if source.exists() or source.is_symlink() or source.is_junction():raise FileExistsError('旧资料位置已被其他文件占用')
    source.mkdir()
    from ctypes import wintypes as w
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateFileW.argtypes=[w.LPCWSTR,w.DWORD,w.DWORD,ctypes.c_void_p,w.DWORD,w.DWORD,w.HANDLE];kernel.CreateFileW.restype=w.HANDLE
    kernel.DeviceIoControl.argtypes=[w.HANDLE,w.DWORD,ctypes.c_void_p,w.DWORD,ctypes.c_void_p,w.DWORD,ctypes.POINTER(w.DWORD),ctypes.c_void_p]
    kernel.CloseHandle.argtypes=[w.HANDLE]
    handle=kernel.CreateFileW(str(source),0x40000000,0,None,3,0x02200000,None)
    try:
        if handle in (None,ctypes.c_void_p(-1).value):raise OSError(ctypes.get_last_error(),'不能建立旧资料位置兼容链接')
        substitute=('\\??\\'+str(target)).encode('utf-16le');display=str(target).encode('utf-16le')
        tail=substitute+b'\0\0'+display+b'\0\0'
        raw=struct.pack('<IHHHHHH',0xA0000003,8+len(tail),0,0,len(substitute),len(substitute)+2,len(display))+tail
        buffer=ctypes.create_string_buffer(raw);returned=w.DWORD()
        if not kernel.DeviceIoControl(handle,0x000900A4,buffer,len(raw),None,0,ctypes.byref(returned),None):raise OSError(ctypes.get_last_error(),'不能建立旧资料位置兼容链接')
    except BaseException:
        if handle not in (None,ctypes.c_void_p(-1).value):kernel.CloseHandle(handle);handle=None
        if source.is_dir() and not source.is_junction() and not any(source.iterdir()):source.rmdir()
        raise
    finally:
        if handle not in (None,ctypes.c_void_p(-1).value):kernel.CloseHandle(handle)
    if not source.is_junction() or source.resolve()!=target:raise OSError('旧资料位置兼容链接校验失败')

def remove(source,target):
    source=Path(source).absolute();target=Path(target).absolute()
    if not source.is_junction() or source.resolve()!=target.resolve():raise ValueError('旧位置已变化，未移除任何目录')
    os.rmdir(source)  # Removes the verified junction itself, never traverses the target.
