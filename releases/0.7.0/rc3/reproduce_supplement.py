from pathlib import Path
import sys,json,hashlib,subprocess,copy
sys.path[:0]=['src']
from PyInstaller.archive.readers import ZlibArchiveReader
import normal_orders,work_repository as wr
from datetime import date,timedelta
from unittest.mock import patch
out=Path('qa/normal_070/rc3-supplement');out.mkdir(exist_ok=False)
archive=ZlibArchiveReader('build_0.7.0_rc3/work/补货助手/PYZ-00.pyz')
required=['application_window','new_products','new_product_ui','new_product_focus','new_product_focus_ui','normal_orders','normal_exports','normal_smoke','file_intake','batch_service','workbench','shiboken6'];assert all(x in archive.toc for x in required)
payload=wr.loads(Path('qa/normal_070/rc3-normal/payload.json').read_bytes());work=wr.Repository(out/'settings');repo=normal_orders.Repository(work)
a=repo.confirm(payload,['QA_A'],None,{'QA_A':None})[0]
with work.connect() as db:
 saved=normal_orders.decode(db.execute('SELECT payload FROM normal_work_revisions WHERE revision=?',(a['payload']['work_revision'],)).fetchone()[0]);assert saved==payload
changed=copy.deepcopy(payload);changed['temporary_note']='standalone later draft';old=work.get_job(wr.today());work.save_job(wr.today(),changed,old['revision'])
with work.connect() as db:assert normal_orders.decode(db.execute('SELECT payload FROM normal_work_revisions WHERE revision=?',(a['payload']['work_revision'],)).fetchone()[0])==payload
nextday=(date.fromisoformat(wr.today())+timedelta(days=1)).isoformat();new=copy.deepcopy(payload);new['day']=nextday
with patch.object(wr,'today',lambda:nextday):b=repo.confirm(new,['QA_A'],None,{'QA_A':None})[0]
assert b['number']!=a['number'] and b['revision']==1 and repo.get(a['day'],'QA_A')['revision']==1
sha=lambda p:hashlib.sha256(Path(p).read_bytes()).hexdigest()
old_zip=Path('交付_0.6.2_rc1/补货助手_0.6.2_rc1.zip');expected='f0f81c9d95ce298cdb3933c3c28d312be2b6bcd891491517ebef4e6cccd8b9a7';assert sha(old_zip)==expected
r={'passed':True,'source_commit':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),'version':'0.7.0','checks':['signed snapshot retains complete work source/config/focus after later draft save','mocked next day new order V001; previous order intact','old 0.6.2 ZIP preserved','PyInstaller collected new focus/UI/order and spawn dependencies'],'modules':required,'old_zip_sha256':expected,'exe_sha256':sha('交付_0.7.0_rc3/补货助手/补货助手.exe')}
(out/'supplement_report.json').write_text(json.dumps(r,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(r,ensure_ascii=False))
