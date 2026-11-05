"""Run the built EXE, not source, against fresh isolated data and client config."""
import argparse,hashlib,json,os,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]


def main():
    p=argparse.ArgumentParser();p.add_argument('--exe',type=Path,required=True);p.add_argument('--out',type=Path,required=True);a=p.parse_args()
    exe=a.exe.resolve();out=a.out.resolve();out.mkdir(parents=True,exist_ok=False)
    build=json.loads((exe.parent/'_internal/build_info.json').read_text('utf-8'))
    if build['version']!='0.40.0':raise ValueError('Expected 0.40.0')
    paths=[exe,exe.parent/'AI接口/补货助手AI接口.exe',exe.parent/'AI入口/补货助手AI启动器.exe']
    hashes={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    reports={}
    for mode in ('portability','analytics'):
        folder=out/mode
        if mode=='analytics':
            from verify_analytics_0390 import fixture
            folder.mkdir();fixture(folder)
        else:folder.mkdir()
        flag='--automation-portability-smoke' if mode=='portability' else '--analytics-smoke'
        with (folder/'process.log').open('w',encoding='utf-8') as log:
            run=subprocess.run([str(exe),flag,str(folder)],cwd=ROOT,env={**os.environ,'REPLENISHMENT_DATA_DIR':str(folder/'settings'),'QT_QPA_PLATFORM':'windows'},stdout=log,stderr=log,timeout=300)
        reports[mode]=json.loads((folder/('portability-exe.json' if mode=='portability' else 'exe-report.json')).read_text('utf-8'))
        if run.returncode or not reports[mode]['passed'] or not reports[mode]['frozen_exe']:raise AssertionError(reports[mode])
    if any(hashlib.sha256(Path(p).read_bytes()).hexdigest()!=h for p,h in hashes.items()):raise AssertionError('Package changed')
    (out/'report.json').write_text(json.dumps({'passed':True,'build':build,'hashes':hashes,'checks':reports},ensure_ascii=False,indent=2),'utf-8')
    print(json.dumps({'passed':True,'source_commit':build['source_commit'],'reports':list(reports)},ensure_ascii=False))


if __name__=='__main__':main()
