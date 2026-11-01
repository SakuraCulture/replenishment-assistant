"""Build an immutable Git archive without touching unrelated working-tree files.

Same PyInstaller options as build.ps1. New build/delivery directories only.
"""
import argparse,hashlib,json,os,re,subprocess,sys,zipfile,shutil
from datetime import datetime
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--commit',default='HEAD');parser.add_argument('--build-id',required=True);a=parser.parse_args()
    if not re.fullmatch('[a-zA-Z0-9_-]+',a.build_id):raise ValueError('Invalid build id')
    commit=subprocess.check_output(['git','rev-parse','--verify',a.commit+'^{commit}'],cwd=ROOT,text=True).strip()
    raw=subprocess.check_output(['git','show',commit+':src/version.json'],cwd=ROOT)
    version=json.loads(raw)['version']
    build=ROOT/f'build_{version}_{a.build_id}';dist=ROOT/f'交付_{version}_{a.build_id}'
    if build.exists() or dist.exists():raise FileExistsError('Build id exists; choose another id')
    build.mkdir();archive=build/'source.zip';source=build/'source'
    subprocess.run(['git','archive','--format=zip','-o',str(archive),commit],cwd=ROOT,check=True)
    with zipfile.ZipFile(archive) as z:z.extractall(source)
    info={'source_commit':commit,'build_id':a.build_id,'version':version,'built_at':datetime.now().astimezone().isoformat(),'source_archive_sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),'source_mode':'immutable git archive; no working-tree overlay'}
    component=source/'components/aoxiang'
    if component.exists():
        # Execute the bundler from the same immutable archive as the application.
        sys.path.insert(0,str(source/'tools'))
        import aoxiang_bundle
        staged=build/'aoxiang-component'
        info['aoxiang']=aoxiang_bundle.stage(component,ROOT/'components/aoxiang',staged,source_commit=commit,app_version=version)
    (build/'build_info.json').write_text(json.dumps(info,ensure_ascii=False,indent=2),encoding='utf-8')
    env=os.environ.copy();env['PATH']=os.environ['WINDIR']+';'+str(Path(os.environ['WINDIR'])/'System32')+';'+str(Path(sys.executable).parent)
    cmd=[sys.executable,'-m','PyInstaller','--clean','--windowed','--onedir','--noupx','--name','补货助手','--icon',str(source/'src/app.ico')]
    for path in [source/'src/default_config.json',source/'src/default_must_sell.json',source/'src/version.json',build/'build_info.json',source/'src/app.ico']:cmd+=['--add-data',str(path)+';.']
    for name in ['numpy','pandas','matplotlib','scipy','IPython','lxml','tkinter']:cmd+=['--exclude-module',name]
    cmd+=['--distpath',str(dist),'--workpath',str(build/'work'),'--specpath',str(build),str(source/'src/app.py')]
    (build/'command.json').write_text(json.dumps(cmd,ensure_ascii=False,indent=2),encoding='utf-8')
    with (build/'build.log').open('w',encoding='utf-8') as log:
        subprocess.run(cmd,cwd=source,env=env,stdout=log,stderr=log,check=True)
        if component.exists():
            target=dist/'补货助手/_internal/components/aoxiang'
            shutil.copytree(staged,target)
            aoxiang_bundle.verify(target)
        subprocess.run([sys.executable,str(source/'prepare_delivery.py'),'--delivery',str(dist/'补货助手')],cwd=source,env=env,stdout=log,stderr=log,check=True)
    print(json.dumps({'delivery':str(dist),'source_commit':commit,'log':str(build/'build.log')},ensure_ascii=False))


if __name__=='__main__':main()
