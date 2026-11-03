"""Explicit one-account live check. Writes ONLY --qa; physical warehouse remains unverified."""
import argparse,json,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
from catalog_sync_client import Client
from catalog_sync import run_sync
from work_repository import Repository
p=argparse.ArgumentParser();p.add_argument('--qa',required=True);p.add_argument('--binding',required=True);p.add_argument('--export',action='store_true');a=p.parse_args()
root=Path(a.qa).resolve();root.mkdir(parents=True,exist_ok=True)
if not root.is_relative_to(ROOT/'qa'):raise ValueError('live check requires isolated QA folder')
binding=json.loads(Path(a.binding).read_text('utf-8'));client=Client(root/'service');db=Repository(root/'settings')
try:
 client.start();status=client.request('sync.status')
 if not status['accounts']:
  ack=client.request('accounts.probe',warehouse='山东仓',profilePath=binding['actualProfilePath'],edgeExecutable=binding['edgeExecutable'],company='团团甄选供应链（临沂）有限公司')
  while True:
   status=client.request('sync.status');op=status['operations'][ack['operation_id']]
   print(json.dumps({'operation':op['state'],'phase':status['active']},ensure_ascii=False),flush=True)
   if op['state']!='RUNNING':break
   time.sleep(1)
  if op['state']!='SUCCEEDED':raise RuntimeError(op.get('error'))
  (root/'probe.json').write_text(json.dumps(op,ensure_ascii=False,indent=2),encoding='utf-8')
  if not a.export:sys.exit(0)
  # Synthetic warehouse slot ONLY in this independent database. Does not verify physical mapping.
  client.request('accounts.bind',warehouse='山东仓',probe_id=op['id'],mapping_verified=True,expected_version=0)
 if a.export:
  status=client.request('sync.status');pending=next((b for b in status['batches'] if any(not i['adoption'] for i in b['items'])),None)
  if any(b['items'] and all(i['adoption'] for i in b['items']) for b in status['batches']) and not pending:raise RuntimeError('Live check already adopted; do not resubmit')
  def progress(v):
   if hasattr(v,'business_event'):
    state=v.business_event.get('state',{});print(json.dumps({'message':str(v),'phase':state.get('active'),'items':[(i['stage'],i.get('error'),i.get('adoption_error')) for b in state.get('batches',[]) for i in b['items']]},ensure_ascii=False),flush=True)
   else:print(str(v),flush=True)
  result=run_sync(client,root/'settings',db.path,['山东仓'],resume={'batch_id':pending['id'],'item_ids':[i['id'] for i in pending['items'] if not i['adoption']]} if pending else None,progress=progress)
  result['scope']='Linyi account only; SD slot is synthetic QA, NOT verified physical warehouse mapping';(root/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
finally:
 while not client.close(timeout=1):print('Waiting for safe component shutdown',flush=True)
