"""Attach local documentation and installed dependency license texts."""
import sys,shutil,json,argparse,importlib.metadata as md
from pathlib import Path
root=Path(__file__).parent
parser=argparse.ArgumentParser();parser.add_argument('--delivery',type=Path,required=True);args=parser.parse_args()
dest=args.delivery.resolve();assert (dest/'补货助手.exe').is_file()
version=json.loads((root/'src/version.json').read_text(encoding='utf-8'))
build=json.loads((dest/'_internal/build_info.json').read_text(encoding='utf-8-sig'))
assert build['version']==version['version']
shutil.copy2(root/'使用说明.txt',dest/'使用说明.txt')
if version['version'] in {'0.7.1','0.7.2','0.7.3'}:
    (dest/'docs').mkdir(exist_ok=True);shutil.copy2(root/'docs/0.7.1导出优化与批量确认单.md',dest/'docs/0.7.1导出优化与批量确认单.md')
if version['version'] in {'0.7.2','0.7.3'}:shutil.copy2(root/'docs/0.7.2历史明细性能.md',dest/'docs/0.7.2历史明细性能.md')
if version['version']=='0.7.3':shutil.copy2(root/'docs/0.7.3工作链路性能与过滤.md',dest/'docs/0.7.3工作链路性能与过滤.md')
licenses=dest/'licenses';licenses.mkdir(exist_ok=True)
if (root/'third_party_licenses').exists():shutil.copytree(root/'third_party_licenses',licenses,dirs_exist_ok=True)
versions={}
for name in ['PySide6-Essentials','shiboken6','openpyxl','et-xmlfile','pillow','PyInstaller']:
    dist=md.distribution(name);versions[name]=dist.version
    (licenses/name).mkdir(exist_ok=True)
    (licenses/name/'package-metadata.txt').write_text(dist.read_text('METADATA') or '',encoding='utf-8')
    for entry in dist.files or []:
        if any(token in str(entry).lower() for token in ['license','licence','copying']) and ('dist-info' in str(entry) or '.dist-info' in str(entry)):
            src=Path(dist.locate_file(entry))
            if src.is_file():
                sub=licenses/name/str(entry).split('.dist-info/',1)[-1]
                sub.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(src,sub)
python_license=Path(sys.base_prefix)/'LICENSE.txt'
if python_license.exists():shutil.copy2(python_license,licenses/'Python-LICENSE.txt')
(dest/'版本信息.json').write_text(json.dumps({**version,**build,'python':sys.version,'packages':versions},ensure_ascii=False,indent=2),encoding='utf-8')
(licenses/'组件来源.txt').write_text('Python: https://www.python.org/\nQt / PySide6 (unmodified dynamic libraries): https://download.qt.io/official_releases/QtForPython/pyside6/\nQt: https://download.qt.io/official_releases/qt/\nopenpyxl: https://openpyxl.readthedocs.io/\nPillow: https://python-pillow.org/\nPyInstaller: https://pyinstaller.org/\n随包Qt/PySide6以动态库方式使用；可用兼容版本库替换_internal内的对应文件。第三方组件适用随包各自许可。\n',encoding='utf-8')
print(json.dumps(versions))
