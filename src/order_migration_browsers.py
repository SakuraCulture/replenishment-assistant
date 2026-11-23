"""Check the local JST profiles that the order migration must preserve."""
import ctypes
import json
import os
import shutil
import subprocess
from ctypes import wintypes
from pathlib import Path

from migration_paths import io_path
from runtime_support import checkpoint


WAREHOUSES = {'hb': '河北仓', 'sd': '山东仓'}
SHARING_ERRORS = (32, 33)


class BrowserProfileBusy(ValueError):
    def __init__(self, warehouses):
        self.warehouses = tuple(sorted(set(warehouses)))
        names = '、'.join(self.warehouses)
        super().__init__(f'{names}聚水潭专用浏览器仍占用本机资料。请等待该仓作业结束，正常退出对应的专用浏览器，再重选原迁移包。')


def _profiles(root):
    root = io_path(Path(root).resolve())
    found = set()

    def add(warehouse, data_dir, profile):
        try:
            data_dir, profile = Path(data_dir), Path(profile)
            if not data_dir.is_absolute() or not profile.is_absolute():
                return
            data_dir, profile = io_path(data_dir.resolve()), io_path(profile.resolve())
        except (OSError, RuntimeError, ValueError):
            return
        if data_dir != root and data_dir.is_relative_to(root) and profile.parent == data_dir and data_dir.is_dir():
            found.add((warehouse, data_dir, profile))

    for folder_name in ('jst_catalog_sync', 'jst_catalog'):
        folder = root/folder_name
        for suffix, warehouse in WAREHOUSES.items():
            data_dir = folder/'browsers'/suffix/'User Data'
            add(warehouse, data_dir, data_dir/'Default')
        try:
            jobs = folder/'jobs.json'
            if not io_path(jobs.resolve()).is_relative_to(root):
                continue
            accounts = json.loads(jobs.read_text('utf-8-sig')).get('accounts', {})
        except (OSError, RuntimeError, ValueError, AttributeError):
            continue
        if not isinstance(accounts, dict):
            continue
        for warehouse, account in accounts.items():
            binding = account.get('binding') if isinstance(account, dict) else None
            if warehouse in WAREHOUSES.values() and isinstance(binding, dict):
                data_dir, profile = binding.get('userDataRoot'), binding.get('actualProfilePath')
                if isinstance(data_dir, str) and isinstance(profile, str):
                    add(warehouse, data_dir, profile)
    return found


def _running_data_dirs():
    from application_processes import arguments
    from data_relocation import browser_rows

    try:
        rows = browser_rows()
    except (OSError, ValueError, subprocess.SubprocessError):
        return set()
    active = set()
    for row in rows or ():
        command = row.get('CommandLine')
        if not command:
            continue
        try:
            args = arguments(command)
            for index, arg in enumerate(args):
                flag, equals, value = arg.partition('=')
                if flag.lower() != '--user-data-dir':
                    continue
                path = Path(value if equals else args[index+1])
                if path.is_absolute():
                    active.add(io_path(path.resolve()))
        except (IndexError, OSError, ValueError, RuntimeError):
            continue
    return active


def _locked_profiles(profiles):
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    open_file = kernel.CreateFileW
    open_file.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                          wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    open_file.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    invalid_handle = ctypes.c_void_p(-1).value
    locked = set()
    for warehouse, _, profile in profiles:
        candidates = [profile/'Network'/name for name in ('Cookies', 'Cookies-journal')]
        for name in ('Sessions', 'Sessions_Encrypted'):
            candidates.extend((profile/name).glob('Session_*'))
            candidates.extend((profile/name).glob('Tabs_*'))
        for path in candidates:
            # OPEN_EXISTING, read access with read/write/delete sharing. Probe
            # only the handle and close it without reading any browser data.
            handle = open_file(str(path), 0x80000000, 7, None, 3, 0, None)
            if handle != invalid_handle:
                kernel.CloseHandle(handle)
            elif ctypes.get_last_error() in SHARING_ERRORS:
                locked.add(warehouse)
                break
    return locked


def require_closed(root, progress=lambda _: None, cancel=None):
    checkpoint(cancel)
    if os.name != 'nt':
        return
    profiles = _profiles(root)
    if not profiles:
        return
    progress('检查聚水潭专用浏览器是否占用本机迁移资料…')
    active = _running_data_dirs()
    checkpoint(cancel)
    locked = {warehouse for warehouse, data_dir, _ in profiles if data_dir in active}
    locked.update(_locked_profiles(profiles))
    if locked:
        raise BrowserProfileBusy(locked)


def copy_local_tree(root, new, *, ignore):
    try:
        shutil.copytree(io_path(root), io_path(new), ignore=ignore)
    except shutil.Error as exc:
        # Covers a browser reopened after the preflight. Retain the original
        # detailed copy error in the migration diagnostics.
        problems = exc.args[0]
        if isinstance(problems, list):
            locked_paths = [
                io_path(source) for source, _, reason in problems
                if any(f'[WinError {code}]' in str(reason) for code in SHARING_ERRORS)
            ]
            warehouses = {warehouse for warehouse, data_dir, _ in _profiles(root)
                          if any(path.is_relative_to(io_path(data_dir)) for path in locked_paths)}
            if warehouses:
                raise BrowserProfileBusy(warehouses) from exc
        raise
