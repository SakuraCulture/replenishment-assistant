"""Merge only on a full target copy after safe application shutdown.

The source package is already verified by order_migration.prepare. No live
business file is edited here: a recoverable same-volume directory handoff
publishes the fully verified copy and retains the complete old root.
"""
import shutil,sqlite3
from contextlib import ExitStack,closing,contextmanager
from pathlib import Path
from application_reset import _exclusive,_check_other_processes
from stockout_common import atomic_json,file_hash,now,resource_check
import order_migration as migration


def prepare(root,package,progress=lambda _:None,cancel=None):
    return migration.prepare(root,package,progress,cancel,mode='merge')


@contextmanager
def _clone(root,new,progress):
    # Block existing writers while SQLite's own backup reads committed content.
    # A backup is required even if an otherwise clean source uses WAL mode.
    databases=[name for name in sorted(migration.DBS) if (root/name).is_file()]
    excluded=set(databases)|migration.DERIVED|{name+suffix for name in migration.DBS|migration.DERIVED for suffix in ('-wal','-shm','-journal')}
    def ignore(folder,names):
        result=[]
        for name in names:
            path=Path(folder)/name
            if path.is_symlink() or path.is_junction():raise ValueError('本机资料含链接，未合并；原资料保持')
            if path.relative_to(root).as_posix() in excluded:result.append(name)
        return result
    with ExitStack() as locks:
        try:
            for name in databases:
                db=locks.enter_context(closing(sqlite3.connect((root/name).as_uri()+'?mode=rw',uri=True,timeout=.3,isolation_level=None)))
                db.execute('BEGIN IMMEDIATE')
        except sqlite3.DatabaseError as exc:raise ValueError('本机订单或关联任务仍被写入，未合并；请安全结束原任务后重试') from exc
        progress('复制本机资料至独立合并副本；原目录保持')
        shutil.copytree(root,new,ignore=ignore)
        for name in databases:
            progress('备份并完整校验本机数据库：'+name)
            migration.db_copy(root/name,new/name,main=name=='workbench.sqlite3',progress=progress)
        yield locks


def apply(plan):
    root=migration.checked_plan(plan);stage=Path(plan['stage']);new=Path(plan['new']);backup=Path(plan['backup'])
    if plan.get('mode')!='merge' or not isinstance(plan.get('merge_report'),dict):raise ValueError('合并预览凭据缺失，未写入资料')
    if not stage.is_dir() or new.exists() or backup.exists():raise ValueError('合并工作目录已变化，原资料保持')
    for entry in plan['manifest']['entries']:
        path=stage/entry['path']
        if not migration.owned(entry['path']) or not path.resolve().is_relative_to(stage.resolve()) or file_hash(path)!=entry['sha256']:
            raise ValueError('迁入材料在预览后变化，原资料保持：'+entry['path'])
    from migration_status import record
    from order_merge import merge
    with _exclusive(root):
        record(plan,'APPLYING',phase='WAITING_LOCKS');_check_other_processes()
        resource_check(root.parent,sum(p.stat().st_size for p in root.rglob('*') if p.is_file())+sum(e['size'] for e in plan['manifest']['entries']))
        record(plan,'APPLYING',phase='COPYING')
        with _clone(root,new,lambda message:record(plan,'APPLYING',phase='COPYING')) as locks:
            record(plan,'APPLYING',phase='MERGING')
            result=merge(stage,new,expected_report=plan['merge_report'],final_root=root)
            # A second complete check is independent of graph insertion logic.
            verified=new/'workbench.sqlite3.merge-verified'
            migration.db_copy(new/'workbench.sqlite3',verified,main=True);verified.unlink()
            for name in migration.DBS-{'workbench.sqlite3'}:
                path=new/name
                if path.is_file():
                    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db:
                        if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('合并副本数据库校验失败：'+name)
            reports=new/'order-merge-reports';reports.mkdir(exist_ok=True)
            receipt={'nonce':plan['nonce'],'at':now(),'mode':'merge','package_sha256':plan['package_sha256'],'backup':str(backup),'scope':'只新增无冲突订单；完全重复跳过，冲突保留本机；不补齐、不覆写','report':result}
            atomic_json(reports/(plan['nonce']+'.json'),receipt)
            atomic_json(new/'订单迁入完成.json',receipt)
            # Drop SQLite handles only at the guarded directory handoff; the
            # instance lease remains exclusive until publication completes.
            locks.close()
            record(plan,'APPLYING',phase='REPLACING');atomic_json(migration.journal(root),plan)
            try:root.rename(backup);new.rename(root)
            except BaseException:
                if migration.journal(root).exists():migration._recover(root)
                raise
        value=migration._recover(root)
        value.update(mode='merge',counts=result['counts'],report=str(root/'order-merge-reports'/(plan['nonce']+'.json')))
        record(plan,'COMPLETED',result=value);migration.discard(plan)
        return value
