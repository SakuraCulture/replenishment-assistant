"""Machine-local registration. No database reads and no portable login/session state."""
import copy
import hashlib
import json
import os
import re
import sys
import tempfile
import tomllib
import uuid
from pathlib import Path

from automation_protocol import CommandError


def bridge_command():
    if getattr(sys, 'frozen', False):
        path = Path(sys.executable).parent / 'AI接口/补货助手AI接口.exe'
        if not path.is_file(): raise ValueError('交付包缺少 AI 接口程序，请使用完整交付目录')
        return str(path), []
    return sys.executable, [str(Path(__file__).with_name('automation_bridge.py').resolve())]


def home():
    # QA/source environments do not register in the operator's normal location.
    if os.environ.get('REPLENISHMENT_AI_HOME'): return Path(os.environ['REPLENISHMENT_AI_HOME']).resolve()
    if os.environ.get('REPLENISHMENT_DATA_DIR'): return Path(os.environ['REPLENISHMENT_DATA_DIR']).resolve() / 'ai-clients'
    return Path(os.environ.get('LOCALAPPDATA') or Path.home()/'AppData/Local')/'YoutuanTools/补货助手/AI接入'


def client_path(client):
    if client == 'codex': return Path(os.environ.get('CODEX_HOME') or Path.home()/'.codex')/'config.toml'
    if client == 'workbuddy': return Path.home()/'.workbuddy/mcp.json'
    raise ValueError('不支持的客户端')


def atomic_bytes(path, content, expected=None, check=False):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,temp=tempfile.mkstemp(prefix=path.name+'.',suffix='.tmp',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as f: f.write(content);f.flush();os.fsync(f.fileno())
        if check and (path.read_bytes() if path.exists() else None) != expected:
            raise ValueError('配置已被其他操作修改，请重新预览后配置')
        os.replace(temp,path)
    finally:
        if Path(temp).exists():Path(temp).unlink()


def binding(g):
    from version_info import VERSION
    command,args=bridge_command()
    return {'schema':1,'workspace':g.workspace,'application':VERSION,'command':command,'args':args,
        'connection':str(g.connection_file.resolve())}


def binding_path(g): return home()/g.workspace/'binding.json'


def refresh_registration(g):
    """Refresh only a previously explicitly registered environment when it is enabled."""
    path=binding_path(g)
    if not path.exists(): return
    old=path.read_bytes();data=json.loads(old)
    if data.get('workspace')!=g.workspace:raise ValueError('本机登记环境不一致，请在 AI 协助重新配置')
    atomic_bytes(path,json.dumps(binding(g),ensure_ascii=False,indent=2).encode('utf-8'),old,True)


def planned_entry(g):
    folder=binding_path(g).parent
    if getattr(sys,'frozen',False):command,args=str(folder/'补货助手AI启动器.exe'),[]
    else:command,args=sys.executable,[str(folder/'automation_launcher.py')]
    return {'command':command,'args':args+['--binding',str(binding_path(g))]}


def install_entry(g):
    folder=binding_path(g).parent;folder.mkdir(parents=True,exist_ok=True)
    if getattr(sys,'frozen',False):
        source=Path(sys.executable).parent/'AI入口/补货助手AI启动器.exe'
        if not source.is_file():raise ValueError('缺少 AI 启动器，请使用完整新发行包')
        target=folder/source.name
        if not target.exists() or hashlib.sha256(target.read_bytes()).digest()!=hashlib.sha256(source.read_bytes()).digest():
            try:atomic_bytes(target,source.read_bytes())
            except PermissionError:raise ValueError('AI 启动器正在使用，请在客户端停用补货助手连接后重试') from None
        command,args=str(target),[]
    else:
        target=folder/'automation_launcher.py'
        atomic_bytes(target,Path(__file__).with_name('automation_launcher.py').read_bytes())
        command,args=sys.executable,[str(target)]
    path=binding_path(g)
    atomic_bytes(path,json.dumps(binding(g),ensure_ascii=False,indent=2).encode('utf-8'))
    return planned_entry(g)


def toml_entry(entry):
    return '[mcp_servers.replenishment]\ncommand = '+json.dumps(entry['command'],ensure_ascii=False)+'\nargs = '+json.dumps(entry['args'],ensure_ascii=False)+'\nstartup_timeout_sec = 30\ntool_timeout_sec = 40\n'


def merge_config(client, raw, entry):
    """Preserve unrelated client settings; reject ambiguous TOML rather than rewrite it."""
    text=(raw or b'').decode('utf-8-sig')
    if client=='workbuddy':
        data=json.loads(text) if text.strip() else {}
        if not isinstance(data,dict) or not isinstance(data.get('mcpServers',{}),dict):raise ValueError('WorkBuddy 配置结构异常，未覆盖')
        old=copy.deepcopy(data.get('mcpServers',{}).get('replenishment'))
        data.setdefault('mcpServers',{})['replenishment']=entry
        return json.dumps(data,ensure_ascii=False,indent=2).encode('utf-8'),old
    if client!='codex':raise ValueError('不支持的客户端')
    before=tomllib.loads(text);servers=before.get('mcp_servers',{})
    if not isinstance(servers,dict):raise ValueError('Codex 配置结构异常，未覆盖')
    old=copy.deepcopy(servers.get('replenishment'))
    lines=text.splitlines(keepends=True);keep=[];inside=False
    for line in lines:
        if re.match(r'^\s*\[',line):
            try:
                table=tomllib.loads(line.strip())
                inside='replenishment' in table.get('mcp_servers',{})
            except (ValueError,TypeError):
                # Multi-line arrays and strings are retained; final parse comparison guards them.
                pass
        if not inside:keep.append(line)
    merged=''.join(keep).rstrip()+'\n\n'+toml_entry(entry)
    try:after=tomllib.loads(merged)
    except ValueError:raise ValueError('Codex 配置写法无法安全合并，请选择导出配置片段手动添加') from None
    expected=copy.deepcopy(before);expected.setdefault('mcp_servers',{})['replenishment']={**entry,'startup_timeout_sec':30,'tool_timeout_sec':40}
    if after!=expected:raise ValueError('Codex 配置含特殊表结构，无法仅更新补货助手；请使用导出配置片段')
    return merged.encode('utf-8'),old


def prepare_config(client,path,entry):
    path=Path(path);old=path.read_bytes() if path.exists() else None
    content,previous=merge_config(client,old,entry)
    return {'path':path,'original':old,'content':content,'previous':previous,'client':client}


def apply_config(plan):
    path=plan['path'];old=plan['original']
    if (path.read_bytes() if path.exists() else None)!=old:raise ValueError('配置已被其他操作修改，请重新核对')
    if old==plan['content']:return {'changed':False,'path':str(path),'backup':None}
    backup=None
    if old is not None:
        backup=path.with_name(path.name+'.replenishment-'+uuid.uuid4().hex[:12]+'.bak')
        # Backups stay beside the private client configuration, never in a delivery/report.
        with backup.open('xb') as f:f.write(old)
    atomic_bytes(path,plan['content'],old,True)
    return {'changed':True,'path':str(path),'backup':str(backup) if backup else None}


def load_connection(path,expected_workspace=None):
    try:data=json.loads(Path(path).read_text('utf-8'))
    except FileNotFoundError:raise CommandError('NOT_CONNECTED','连接文件不存在：请在目标资料环境启用本机连接；程序或资料搬家后点击“配置/修复本机连接”') from None
    except (OSError,ValueError):raise CommandError('INVALID_CONNECTION','连接文件无法读取或内容损坏，请在目标工作台关闭后重新启用本机连接') from None
    if not isinstance(data,dict) or any(not isinstance(data.get(k),str) or not data[k] for k in ('protocol','server','token')):
        raise CommandError('INVALID_CONNECTION','连接配置不完整，请在目标工作台重新启用本机连接')
    if expected_workspace and data.get('workspace')!=expected_workspace:
        raise CommandError('WORKSPACE_MISMATCH','连接指向其他资料环境，请重新选择目标工作台配置；未自动切换')
    return data
