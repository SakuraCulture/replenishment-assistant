"""Local UI preference only; never part of model/job/order fingerprints."""
import json
import os
import tempfile
from pathlib import Path

FILENAME = 'export_settings.json'

def load(path):
    path = Path(path)
    if not path.exists(): return ''
    try: value = json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError) as exc: raise ValueError('默认导出位置配置无法读取：' + str(exc)) from exc
    if not isinstance(value, dict) or type(value.get('schema')) is not int or value['schema'] != 1 or not isinstance(value.get('default_export_directory'), str):
        raise ValueError('默认导出位置配置格式不正确')
    directory = value['default_export_directory']
    if directory and not Path(directory).is_absolute(): raise ValueError('默认导出位置必须是绝对路径')
    return directory

def validate(directory, writable=False):
    if not directory: return ''
    p = Path(directory)
    if not p.is_absolute(): raise ValueError('请选择或填写完整的文件夹路径')
    if not p.is_dir(): raise ValueError('导出文件夹不存在或不可访问，请重新选择：' + str(p))
    if writable:
        try:
            with tempfile.TemporaryFile(prefix='.replenishment-write-check-', dir=p): pass
        except OSError as exc: raise ValueError('导出文件夹不可写，请选择其它位置：' + str(p)) from exc
    return str(p.resolve())

def save(path, directory):
    directory = validate(directory.strip(), writable=True)
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.'+path.name+'-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as f:
            json.dump({'schema': 1, 'default_export_directory': directory}, f, ensure_ascii=False, indent=2)
            f.write('\n'); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)
    finally: Path(tmp).unlink(missing_ok=True)
    return directory
