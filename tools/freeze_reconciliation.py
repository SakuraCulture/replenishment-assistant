"""Freeze source and hash protected packages/configuration; never write user data."""
import hashlib,json,os,subprocess,zipfile,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'qa/reconciliation_016'
def sha(p):
    h=hashlib.sha256()
    with Path(p).open('rb') as f:
        for b in iter(lambda:f.read(4*1024*1024),b''):h.update(b)
    return h.hexdigest()
def main():
    OUT.mkdir(parents=True,exist_ok=False)
    commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    subprocess.run(['git','archive','--format=zip','--output',str(OUT/'baseline.zip'),commit],cwd=ROOT,check=True)
    with zipfile.ZipFile(OUT/'baseline.zip') as z:z.extractall(OUT/'baseline')
    protected={}
    roots=[*ROOT.glob('交付_*'),Path(os.environ['LOCALAPPDATA'])/'YoutuanTools/补货助手',ROOT/'qa/verified_092/fixture']
    for folder in roots:
        for p in folder.rglob('*'):
            if p.is_file():protected[str(p)]=sha(p)
    for name,value in [('baseline.json',{'commit':commit,'branch':subprocess.check_output(['git','branch','--show-current'],cwd=ROOT,text=True).strip(),'source_archive_sha':sha(OUT/'baseline.zip')}),('protected.json',protected),('environment.json',{'python':sys.version,'executable':sys.executable,'dependencies':subprocess.check_output([sys.executable,'-m','pip','freeze'],text=True).splitlines()})]:
        (OUT/name).write_bytes((json.dumps(value,ensure_ascii=False,indent=2)+'\n').encode())
    print(json.dumps({'commit':commit,'protected_files':len(protected)}))
if __name__=='__main__':main()
