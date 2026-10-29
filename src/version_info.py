"""One version source for UI, exports and release tools."""
import json
from pathlib import Path

INFO = json.loads((Path(__file__).parent / 'version.json').read_text(encoding='utf-8'))
VERSION = INFO['version']
MODEL_VERSION = INFO['model_version']
BUILD_FILE = Path(__file__).parent / 'build_info.json'
BUILD = json.loads(BUILD_FILE.read_text(encoding='utf-8')) if BUILD_FILE.exists() else {'source_commit': '源码运行', 'build_id': '未打包'}

def diagnostic():
    return {**INFO, **BUILD}
