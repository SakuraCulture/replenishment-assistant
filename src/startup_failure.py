"""Keep an actionable startup error visible even if Qt could not initialize."""
import ctypes
import json
import os
import tempfile
import traceback
from datetime import datetime
from pathlib import Path


def save_failure(error):
    record = {
        'at': datetime.now().astimezone().isoformat(),
        'pid': os.getpid(),
        'event': 'STARTUP_FAILED',
        'type': type(error).__name__,
        'message': str(error),
        'traceback': ''.join(traceback.format_exception(error))[-32000:],
    }
    locations = []
    if os.environ.get('LOCALAPPDATA'):
        locations.append(Path(os.environ['LOCALAPPDATA']) / 'YoutuanTools/data-location/diagnostics')
    try:
        locations.append(Path(tempfile.gettempdir()) / 'YoutuanTools/startup')
    except OSError:
        pass
    for folder in locations:
        temporary = None
        try:
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / 'last-startup-failure.json'
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', prefix='startup-',
                                             suffix='.tmp', dir=folder, delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(record, stream, ensure_ascii=False)
            os.replace(temporary, target)
            return target
        except OSError:
            continue
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
    return None


def show_failure(error, path):
    reason = str(error).strip() or type(error).__name__
    if reason in ('file is not a database', 'database disk image is malformed'):
        reason = '已有资料库无法读取。请保留原资料目录。\n' + reason
    elif isinstance(error, ImportError):
        reason = '启动组件未能加载。\n' + reason
    message = reason[:1400]
    if path is not None:
        message += '\n\n启动记录\n' + str(path)
    if os.name == 'nt':
        # A Windows dialog still works when creating the Qt application failed.
        ctypes.windll.user32.MessageBoxW(None, message, '补货助手启动未完成', 0x10010)
