"""Whole local-data reset by same-volume rename, after the application has closed.

The sibling backup is never deleted. No database, external file, browser profile
or platform API is edited by this module. A sibling journal makes interrupted
renames recoverable before the application opens its repositories.
"""
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path


def validate_root(root):
    root = Path(root).absolute()
    if root.is_symlink() or (root.exists() and root.is_junction()):
        raise ValueError('资料目录不能是链接或目录联接，请使用独立的应用资料目录')
    root = root.resolve()
    forbidden = {Path(root.anchor), Path.home(), Path(__file__).resolve().parent,
                 Path(__file__).resolve().parent.parent}
    for name in ('USERPROFILE', 'WINDIR', 'ProgramFiles', 'LOCALAPPDATA', 'APPDATA'):
        if os.environ.get(name): forbidden.add(Path(os.environ[name]).resolve())
    if root in forbidden or any(root in p.parents for p in forbidden):
        raise ValueError('不能初始化系统、用户或源码根目录；请指定独立应用资料目录')
    if (root / '.git').exists() or (root / '补货助手.exe').exists() or (root / 'src' / 'app.py').exists():
        raise ValueError('资料目录包含程序或源码，不能整体初始化')
    return root


def _paths(root):
    return root.parent / ('.' + root.name + '.reset.json'), root.parent / ('.' + root.name + '.instances.sqlite3')


def _write(path, value):
    tmp = path.with_name(path.name + '.tmp-' + uuid.uuid4().hex)
    try:
        with tmp.open('x', encoding='utf-8') as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists(): tmp.unlink()


def _connection(root):
    root.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(_paths(root)[1], timeout=.3, isolation_level=None)
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='app_instance'").fetchone():
        db.execute('CREATE TABLE IF NOT EXISTS app_instance (id INTEGER PRIMARY KEY)')
        db.execute('INSERT OR IGNORE INTO app_instance VALUES(1)')
    return db


class InstanceLease:
    """Shared read lock: multiple windows remain supported, reset is exclusive."""
    def __init__(self, root):
        self.db = _connection(Path(root).resolve())
        self.db.execute('BEGIN'); self.db.execute('SELECT * FROM app_instance').fetchone()

    def close(self):
        if self.db is not None:
            self.db.close(); self.db = None


@contextmanager
def _exclusive(root):
    db = _connection(root)
    try:
        try: db.execute('BEGIN EXCLUSIVE')
        except sqlite3.OperationalError as exc:
            raise ValueError('另一个补货助手窗口仍在使用此资料目录，请关闭后再初始化') from exc
        yield
    finally: db.close()


def plan(root):
    root = validate_root(root)
    if not root.is_dir(): raise ValueError('应用资料目录不存在，未初始化')
    nonce = uuid.uuid4().hex
    backup = root.with_name(root.name + '.初始化备份-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + nonce[:8])
    return {'root': str(root), 'backup': str(backup), 'nonce': nonce, 'state': 'PREPARED'}


def _validate_plan(value):
    root = validate_root(value['root']); backup = Path(value['backup']).absolute()
    if backup.parent != root.parent or not backup.name.startswith(root.name + '.初始化备份-'):
        raise ValueError('初始化备份位置无效，未移动资料')
    if backup.is_symlink() or (backup.exists() and backup.is_junction()):
        raise ValueError('初始化备份不能是目录链接')
    if not value.get('nonce'): raise ValueError('初始化凭据缺失')
    return root, backup


def _recover(root):
    journal = _paths(root)[0]
    if not journal.exists(): return None
    value = json.loads(journal.read_text(encoding='utf-8'))
    target, backup = _validate_plan(value)
    if target != root: raise ValueError('初始化记录与当前资料目录不一致')
    receipt = root / '初始化完成.json'
    committed = receipt.is_file() and json.loads(receipt.read_text(encoding='utf-8')).get('nonce') == value['nonce']
    if committed:
        if not backup.is_dir(): raise ValueError('初始化备份缺失，请检查：' + str(backup))
        journal.unlink(); return 'COMPLETED'
    if backup.is_dir():
        if root.exists():
            if any(root.iterdir()): raise ValueError('初始化被中断，原资料和新目录均已保留，请检查：' + str(backup))
            root.rmdir()  # Only the empty, exact fresh directory can be removed.
        backup.rename(root)
        journal.unlink(); return 'ROLLED_BACK'
    if root.is_dir():
        journal.unlink(); return 'UNCHANGED'
    raise ValueError('初始化记录的原目录与备份均不存在，请检查：' + str(journal))


def recover(root):
    root = validate_root(root)
    if not _paths(root)[0].exists(): return None
    with _exclusive(root): return _recover(root)


def _check_other_processes():
    # Old releases predate the shared instance lease. Only inspect application
    # process identities, and never terminate a process or print command lines.
    if os.name != 'nt': return
    from application_processes import discover
    matches=discover()
    if matches:raise ValueError('其他补货助手进程仍在运行，请先正常关闭；资料未替换。进程号：'+','.join(str(p['pid']) for p in matches))


def perform(value):
    """Call only after accepted window close, repository shutdown and log close."""
    root, backup = _validate_plan(value)
    with _exclusive(root):
        _check_other_processes()
        _recover(root)
        if not root.is_dir() or backup.exists(): raise ValueError('资料或备份目录已变化，未初始化')
        # Older releases do not hold InstanceLease. Their open SQLite writers
        # and directory handles must also allow the rename; never kill them.
        connections = []
        try:
            for folder, dirs, files in os.walk(root, followlinks=False):
                dirs[:] = [n for n in dirs if not (Path(folder)/n).is_symlink() and not (Path(folder)/n).is_junction()]
                for name in files:
                    path = Path(folder)/name
                    if path.is_symlink() or path.suffix not in ('.sqlite', '.sqlite3', '.db'): continue
                    db = sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, timeout=.1, isolation_level=None)
                    connections.append(db); db.execute('BEGIN EXCLUSIVE')
        except Exception as exc:
            raise ValueError('仍有数据库被占用，请等作业安全结束并关闭其他窗口后重试；资料未清空') from exc
        finally:
            for db in connections: db.close()
        journal = _paths(root)[0]
        _write(journal, value)
        try:
            root.rename(backup)
            root.mkdir()
            _write(root / '初始化完成.json', {**value, 'state': 'COMPLETED'})
        except Exception:
            _recover(root)
            raise
        try: journal.unlink()
        except OSError: pass  # The durable receipt lets startup finish this cleanup.
        return {**value, 'state': 'COMPLETED'}
