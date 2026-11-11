"""Read fallback for immutable paths retained by migrated snapshots.

Existing user-selected files always win. This never rewrites a signed payload,
changes a hash, moves a file, or redirects a write.
"""
import json,os
from pathlib import Path
_cache={}

def resolve(value,*,mapping=None):
    path=Path(value)
    if path.is_file():return path
    configured=mapping or os.environ.get('REPLENISHMENT_PORTABLE_PATHS')
    if not configured:return path
    marker=Path(configured)
    if not marker.is_file():return path
    stat=marker.stat();key=(str(marker),stat.st_mtime_ns,stat.st_size)
    if stat.st_size>16*1024**2:raise ValueError('迁入原文件映射过大')
    if key not in _cache:
        value=json.loads(marker.read_text('utf-8'));_cache.clear();_cache[key]=value
    record=_cache[key];relative=record.get('paths',{}).get(str(path))
    if relative is None:return path
    target=(marker.parent/relative).resolve()
    if Path(relative).is_absolute() or not target.is_relative_to(marker.parent.resolve()):raise ValueError('迁入文件映射越出资料目录，未读取')
    return target
