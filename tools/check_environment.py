"""Read-only development environment check; report is written only when requested."""
import argparse,hashlib,importlib.metadata as metadata,json,os,re,struct,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def inspect():
    lock=ROOT/'requirements-win-amd64.lock';packages=[];errors=[]
    for line in lock.read_text(encoding='utf-8').splitlines():
        match=re.match(r'^([\w.-]+)==([^\s]+)',line)
        if not match:continue
        name,wanted=match.groups()
        try:actual=metadata.version(name)
        except metadata.PackageNotFoundError:actual=None
        packages.append({'name':name,'locked':wanted,'installed':actual,'matches':actual==wanted})
        if actual!=wanted:errors.append(f'{name}: expected {wanted}, installed {actual}')
    if sys.version_info[:3]!=(3,12,14) or struct.calcsize('P')!=8 or sys.platform!='win32':errors.append('Expected Windows x64 CPython 3.12.14')
    if Path(sys.prefix).resolve()!=(ROOT/'.venv').resolve():errors.append('Development entry expects project .venv; do not use global Python or an old QA environment')
    qt={}
    try:
        from PySide6 import QtCore,QtWidgets
        import openpyxl,PyInstaller
        qt={'version':QtCore.qVersion(),'core_file':QtCore.__file__,'widgets_loaded':bool(QtWidgets.QApplication)}
    except Exception as exc:errors.append('Runtime import failed: '+repr(exc))
    pip=subprocess.run([sys.executable,'-m','pip','check'],capture_output=True,text=True)
    if pip.returncode:errors.append('pip check failed: '+pip.stdout+pip.stderr)
    def git(*args):
        r=subprocess.run(['git',*args],cwd=ROOT,capture_output=True,text=True,encoding='utf-8')
        if r.returncode:raise RuntimeError(r.stderr.strip())
        return r.stdout.strip()
    return {'passed':not errors,'errors':errors,'python':sys.version,'executable':sys.executable,
            'prefix':sys.prefix,'lock_sha256':hashlib.sha256(lock.read_bytes()).hexdigest(),
            'packages':packages,'runtime_imports':qt,'pip_check':pip.stdout.strip(),
            'git':{'branch':git('branch','--show-current'),'commit':git('rev-parse','HEAD'),'status':git('status','--short')},
            'application':json.loads((ROOT/'src/version.json').read_text(encoding='utf-8')),
            'data_dir':os.environ.get('REPLENISHMENT_DATA_DIR'),
            'limits':'Installed versions and runtime imports checked. This is not installed-file hash verification, a fresh-PC test or business acceptance.'}

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--report',type=Path);args=parser.parse_args();report=inspect()
    if args.report:
        args.report.parent.mkdir(parents=True,exist_ok=True)
        with args.report.open('x',encoding='utf-8',newline='\n') as f:json.dump(report,f,ensure_ascii=False,indent=2)
    print('Environment: '+('PASS' if report['passed'] else 'FAIL'))
    print('Python: '+report['executable']);print('Locked packages: '+str(len(report['packages'])))
    for error in report['errors']:print(error)
    if args.report:print('Report: '+str(args.report.resolve()))
    return 0 if report['passed'] else 1

if __name__=='__main__':sys.exit(main())
