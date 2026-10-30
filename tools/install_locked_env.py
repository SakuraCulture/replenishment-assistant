"""Create a NEW Windows x64 build environment; never alter an existing venv."""
import argparse,subprocess,sys,venv
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('destination',type=Path);p.add_argument('--wheels',type=Path);a=p.parse_args()
if sys.platform!='win32' or sys.maxsize<=2**32:raise RuntimeError('此锁文件针对 Windows x64')
dest=a.destination.resolve()
if dest.exists():raise FileExistsError('目标已存在，不覆盖：'+str(dest))
venv.EnvBuilder(with_pip=True).create(dest)
cmd=[str(dest/'Scripts/python.exe'),'-m','pip','install','--require-hashes','-r',str(Path(__file__).resolve().parents[1]/'requirements-win-amd64.lock')]
if a.wheels:cmd+=['--no-index','--find-links',str(a.wheels.resolve())]
subprocess.run(cmd,check=True);subprocess.run([str(dest/'Scripts/python.exe'),'-m','pip','check'],check=True)
print('构建命令：powershell -File build.ps1 -BuildId 新编号 -PythonPath "'+str(dest/'Scripts/python.exe')+'"')
