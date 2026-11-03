"""Verify isolated component bytes and the exact shared read-only AX libraries."""
import hashlib,json
from pathlib import Path

def digest(path):
    with Path(path).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()

def verify_component(jst,ax):
    jst,ax=Path(jst),Path(ax);manifest=json.loads((jst/'integrity.manifest.json').read_text('utf-8'))
    if manifest.get('schema')!=1:raise ValueError('聚水潭组件清单格式异常')
    files={p.relative_to(jst).as_posix():p for p in jst.rglob('*') if p.is_file()}
    if set(files)!=set(manifest['files'])|{'integrity.manifest.json'}:raise ValueError('聚水潭组件文件缺失或混入未知文件')
    for root,entries in [(jst,manifest['files']),(ax,manifest['shared'])]:
        for name,sha in entries.items():
            p=(root/name).resolve()
            if not p.is_relative_to(root.resolve()) or digest(p)!=sha:raise ValueError('采集组件校验失败：'+name)
    return manifest
