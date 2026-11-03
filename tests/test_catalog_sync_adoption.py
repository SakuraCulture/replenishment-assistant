import copy,json,sqlite3,sys,tempfile,threading,unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
import openpyxl
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import warehouse_catalog as wc
import catalog_sync_validation as v
import catalog_sync as s
import work_repository as wr
import jst_orders as j
from runtime_support import Cancelled

class Adoption(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.settings=self.root/'settings';self.repo=wr.Repository(self.settings);self.node=self.root/'node';self.folder=self.node/'downloads/batch/item';self.folder.mkdir(parents=True)
  f=self.folder/'source.xlsx';b=openpyxl.Workbook();b.active.append(wc.REQUIRED+['商品名称']);b.active.append(['000123','快销',2,24,'启用',240,'合成品']);b.save(f);b.close()
  self.receipt=v.validate({'schema':1,'file':str(f),'warehouse':'山东仓','sha256':v.digest(f),'expected_rows':1,'candidate':str(self.folder/'candidate.json')})
  self.batch={'id':'batch','purpose':'default_catalog'};self.item={'id':'item','account':{'warehouse':'山东仓','expectedCoid':'1','expectedUid':'2'},'receipt':self.receipt,'native':{'sha256':self.receipt['source_sha256'],'scope':{'coid':'1','uid':'2'},'nativeTaskRef':'123'},'expected_catalog_digest':None}
  self.ad=s.Adoptions(self.settings,self.repo.path,self.node)
 def tearDown(self):self.tmp.cleanup()
 def test_same_manual_rows_original_bytes_and_retry(self):
  ack=self.ad.apply(self.batch,self.item);saved=wc.Repository(self.settings).catalogs()['山东仓'];self.assertEqual(saved,v.candidate(self.receipt))
  with closing(sqlite3.connect(self.repo.path)) as db:self.assertEqual(db.execute('SELECT content FROM source_files').fetchone()[0],Path(self.receipt['source']).read_bytes());self.assertFalse(j.exists(db))
  digest=self.ad.catalogs.current_digest('山东仓');self.assertEqual(self.ad.apply(self.batch,self.item),ack);self.assertEqual(digest,self.ad.catalogs.current_digest('山东仓'))
 def test_same_bytes_do_not_replace_default_metadata(self):
  self.ad.apply(self.batch,self.item);before=self.ad.catalogs.current_digest('山东仓');data=self.ad.catalogs.catalogs()['山东仓'];self.item['expected_catalog_digest']=before
  # Separate task directory with exactly the same source bytes and new imported time.
  import shutil
  folder=self.node/'downloads/next/item';folder.mkdir(parents=True);file=folder/'same.xlsx';shutil.copyfile(self.receipt['source'],file)
  self.item['receipt']=v.validate({'schema':1,'file':str(file),'warehouse':'山东仓','sha256':v.digest(file),'expected_rows':1,'candidate':str(folder/'candidate.json')})
  ack=self.ad.apply({'id':'next','purpose':'default_catalog'},self.item);self.assertTrue(ack['unchanged']);self.assertEqual(before,self.ad.catalogs.current_digest('山东仓'));self.assertEqual(data,self.ad.catalogs.catalogs()['山东仓'])
 def test_manual_update_during_download_rejected(self):
  changed=v.candidate(self.receipt);changed['imported_at']='newer';self.ad.catalogs.save(changed);digest=self.ad.catalogs.current_digest('山东仓')
  with self.assertRaises(wr.Conflict):self.ad.apply(self.batch,self.item)
  self.assertEqual(digest,self.ad.catalogs.current_digest('山东仓'))
 def test_cancel_before_and_at_commit_preserves_old(self):
  stop=threading.Event();stop.set()
  with self.assertRaises(Cancelled):self.ad.apply(self.batch,self.item,cancel=stop)
  self.assertIsNone(self.ad.catalogs.current_digest('山东仓'));stop.clear()
  def progress(m):
   if getattr(m,'active',False):stop.set()
  with self.assertRaises(Cancelled):self.ad.apply(self.batch,self.item,progress,stop)
  self.assertIsNone(self.ad.catalogs.current_digest('山东仓'));self.assertEqual(self.ad.recover()[0]['state'],'READY')
  stop.clear();self.ad.apply(self.batch,self.item,cancel=stop);self.assertIsNotNone(self.ad.catalogs.current_digest('山东仓'))
 def test_crash_after_replace_recovers_without_replacing_newer_data(self):
  real=self.ad.mark
  with patch.object(self.ad,'mark',side_effect=RuntimeError('simulated crash after replace')):
   with self.assertRaises(RuntimeError):self.ad.apply(self.batch,self.item)
  self.assertEqual(self.ad.recover()[0]['state'],'APPLIED');ack=self.ad.apply(self.batch,self.item)
  changed=v.candidate(self.receipt);changed['imported_at']='newer';self.ad.catalogs.save(changed);digest=self.ad.catalogs.current_digest('山东仓');self.assertEqual(self.ad.apply(self.batch,self.item),ack);self.assertEqual(digest,self.ad.catalogs.current_digest('山东仓'))
 def test_identity_and_source_changes_rejected(self):
  wrong=copy.deepcopy(self.item);wrong['native']['scope']['coid']='9'
  with self.assertRaises(ValueError):self.ad.apply(self.batch,wrong)
  Path(self.receipt['source']).write_bytes(b'bad')
  with self.assertRaises(ValueError):self.ad.apply(self.batch,self.item)
  self.assertIsNone(self.ad.catalogs.current_digest('山东仓'))
 def test_default_does_not_reset_existing_inventory(self):
  data=v.candidate(self.receipt);j.commit_refresh(self.repo.path,j.prepare_refresh(self.repo.path,'山东仓',catalog=data))
  before=s.stock_observation(self.repo.path,'山东仓');self.ad.apply(self.batch,self.item);after=s.stock_observation(self.repo.path,'山东仓');self.assertEqual(before['state'],after['state']);self.assertEqual(before['included'],after['included'])
 def test_allocation_refresh_rejects_changed_state_and_preserves_default(self):
  self.batch['purpose']='allocation_baseline';self.item['observation']=s.stock_observation(self.repo.path,'山东仓');data=v.candidate(self.receipt);j.commit_refresh(self.repo.path,j.prepare_refresh(self.repo.path,'山东仓',catalog=data))
  with self.assertRaises(wr.Conflict):self.ad.apply(self.batch,self.item)
  self.assertIsNone(self.ad.catalogs.current_digest('山东仓'))
 def test_allocation_refresh_idempotent(self):
  self.batch['purpose']='allocation_baseline';self.item['observation']=s.stock_observation(self.repo.path,'山东仓');ack=self.ad.apply(self.batch,self.item);before=s.stock_observation(self.repo.path,'山东仓');self.assertEqual(self.ad.apply(self.batch,self.item),ack);self.assertEqual(before['state'],s.stock_observation(self.repo.path,'山东仓')['state']);self.assertIsNone(self.ad.catalogs.current_digest('山东仓'))

if __name__=='__main__':unittest.main()
