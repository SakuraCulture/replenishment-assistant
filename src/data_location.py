"""Machine-local data location. A missing configured disk never creates an empty store."""
import json,os,re
from pathlib import Path

SCHEMA=1
MARKER='.data-location.json'

def default_base():
    return Path(os.environ.get('LOCALAPPDATA') or Path.home()/'AppData/Local')/'YoutuanTools/补货助手'

def control():
    # Deliberately outside the movable application directory; only small pointers/logs.
    return Path(os.environ.get('LOCALAPPDATA') or Path.home()/'AppData/Local')/'YoutuanTools/data-location'

def config_file():return control()/'location.json'
def journal_file():return control()/'move.json'

def absolute(value):
    if not isinstance(value,(str,os.PathLike)):raise ValueError('资料文件夹路径无效')
    p=Path(value)
    if not p.is_absolute() or str(p).startswith(('\\\\','//')):
        raise ValueError('请选择本机磁盘上的完整资料文件夹路径')
    return p.absolute()

def config():
    path=config_file()
    if not path.is_file():return None
    try:
        value=json.loads(path.read_text('utf-8'))
        if value.get('schema')!=SCHEMA or not re.fullmatch('[a-f0-9]{32}',value.get('dataset_id','')):raise ValueError('资料身份无效')
        absolute(value['active_base']);absolute(value['anchor_base'])
        aliases=value.get('aliases',[])
        if not isinstance(aliases,list) or len(aliases)>32:raise ValueError('旧位置记录无效')
        for item in aliases:absolute(item['from']);absolute(item['to'])
        return value
    except (OSError,ValueError,KeyError,TypeError,AttributeError) as exc:
        raise ValueError('数据保存位置配置无法读取；未建立新的空资料库。请保留配置：'+str(path)) from exc

def base(default=None,*,check=True):
    # Explicit callers can supply a default. Automatic application entrypoints
    # use default_base(), never Windows KnownFolder (which ignores LOCALAPPDATA).
    value=config();folder=absolute(value['active_base']) if value else Path(default or default_base()).absolute()
    if value and check:
        try:
            marker=json.loads((folder/MARKER).read_text('utf-8'))
            if marker.get('dataset_id')!=value['dataset_id'] or not (folder/'settings/workbench.sqlite3').is_file():raise ValueError('资料身份或订单库缺失')
        except (OSError,ValueError,TypeError,AttributeError) as exc:
            raise ValueError('已设置的数据位置不可用：'+str(folder)+'\n请接回资料盘或恢复原目录；不会自动切回C盘建立空库。') from exc
    return folder.resolve() if folder.exists() else folder

def root(default=None):
    override=os.environ.get('REPLENISHMENT_DATA_DIR')
    return Path(override).resolve() if override else base(default)/'settings'

def hint(default=None):
    # Before the instance guard/recovery: no mkdir and no configured-disk fallback writes.
    override=os.environ.get('REPLENISHMENT_DATA_DIR')
    if override:return Path(override).absolute()
    try:return base(default,check=False)/'settings'
    except ValueError:return Path(default or default_base())/'settings'

def identity_path(value):
    path=Path(value).resolve()
    if os.environ.get('REPLENISHMENT_DATA_DIR'):return path
    value=config()
    if value:
        active=Path(value['active_base']).resolve()
        if path.is_relative_to(active):return absolute(value['anchor_base'])/path.relative_to(active)
    return path

def aliases_for(folder):
    if os.environ.get('REPLENISHMENT_DATA_DIR'):return []
    value=config()
    if not value:return []
    active=base();path=Path(folder).resolve()
    return list(value.get('aliases',[])) if path.is_relative_to(active) else []

def pending_message():
    try:value=json.loads(journal_file().read_text('utf-8'))
    except (OSError,ValueError):return ''
    if value.get('state') not in ('WAITING_EXIT','APPLYING'):return ''
    return '正在搬迁应用资料：'+value.get('message',value.get('phase','等待安全退出'))+'。请等待完成后重新打开，未开启第二个业务窗口。'
