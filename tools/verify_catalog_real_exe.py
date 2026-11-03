"""Replay real downloaded dual-warehouse tasks through actual EXE buttons.
No platform request; original files unchanged; independent empty app data.
"""
import argparse,copy,json,os,shutil,subprocess,sys,hashlib
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
from catalog_sync_client import Client
def main():
 p=argparse.ArgumentParser();p.add_argument('--live',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--exe',type=Path,required=True);a=p.parse_args()
 out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);settings=out/'user';service=settings/'jst_catalog_sync'
 c=Client(service);c.start();c.close();original=json.loads((a.live/'service/jobs.json').read_text('utf-8'));state=json.loads((service/'jobs.json').read_text('utf-8'))
 batches=list(original['batches'].values())
 if len(batches)!=1 or not all(i.get('adoption') for i in batches[0]['items']):raise ValueError('Requires completed real batch')
 batch=copy.deepcopy(batches[0]);batch['state']='FILES_READY';expected={}
 for i in batch['items']:
  n=i['native'];wh=i['account']['warehouse'];folder=service/'downloads'/batch['id']/i['id'];folder.mkdir(parents=True);f=folder/Path(n['file']).name;shutil.copy2(n['file'],f);n.update(file=str(f),stage='DOWNLOADED');i.update(receipt=None,adoption=None,stage='DOWNLOADED',expected_catalog_digest=None,error=None,adoption_error=None)
  expected[wh]={'rows':n['scope']['expectedRows'],'sha256':n['sha256'],'company':i['account']['expectedCompany']}
 state.update(accounts=original['accounts'],batches={batch['id']:batch});(service/'jobs.json').write_text(json.dumps(state,ensure_ascii=False),'utf-8');(out/'real_catalogs.json').write_text(json.dumps(expected,ensure_ascii=False),'utf-8')
 env=os.environ.copy();env.update(REPLENISHMENT_DATA_DIR=str(settings),PYTHONUTF8='1')
 with (out/'process.log').open('w',encoding='utf-8') as log:r=subprocess.run([str(a.exe.resolve()),'--catalog-sync-smoke',str(out)],env=env,stdout=log,stderr=log,timeout=180)
 report=json.loads((out/'report.json').read_text('utf-8'));report.update(exit_code=r.returncode,exe_sha256=hashlib.sha256(a.exe.read_bytes()).hexdigest());(out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8');print(json.dumps(report,ensure_ascii=False))
 if r.returncode or not report['passed'] or not report['frozen']:raise SystemExit(1)
if __name__=='__main__':main()
