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
 def test_same_original_can_preserve_a_newly_imported_virtual_category(self):
  file=self.folder/'virtual.xlsx';book=openpyxl.Workbook()
  book.active.append(wc.REQUIRED+['虚拟分类'])
  book.active.append(['000123','快销',2,24,'启用',240,'标杆淘汰'])
  book.save(file);book.close()
  digest=v.digest(file)
  receipt=v.validate({'schema':1,'file':str(file),'warehouse':'山东仓','sha256':digest,
                      'expected_rows':1,'candidate':str(self.folder/'virtual.json')})
  old=v.candidate(receipt);old['items']['000123'].pop('虚拟分类');old['revision']=wc.revision(old['items'])
  self.ad.catalogs.save(old)
  item={**self.item,'receipt':receipt,'native':{**self.item['native'],'sha256':digest},
        'expected_catalog_digest':self.ad.catalogs.current_digest('山东仓')}
  ack=self.ad.apply(self.batch,item)
  self.assertNotIn('unchanged',ack)
  self.assertEqual(self.ad.catalogs.catalogs()['山东仓']['items']['000123']['虚拟分类'],'标杆淘汰')
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
 def test_default_publishes_current_and_preserves_legacy_reservations(self):
  data=v.candidate(self.receipt);j.commit_refresh(self.repo.path,j.prepare_refresh(self.repo.path,'山东仓',catalog=data))
  self.item['expected_catalog_digest']=self.ad.catalogs.current_digest('山东仓')
  before=s.stock_observation(self.repo.path,'山东仓');ack=self.ad.apply(self.batch,self.item);after=s.stock_observation(self.repo.path,'山东仓')
  self.assertEqual(before['state'],after['state']);self.assertEqual(before['included'],after['included'])
  self.assertEqual(ack['purpose'],'default_catalog')
  self.assertEqual(self.ad.catalogs.catalogs()['山东仓'],data)
  self.assertEqual(self.ad.catalogs.current_digest('山东仓'),self.ad.get(ack['apply_id'])['new_digest'])
 def test_allocation_refresh_rejects_changed_state_and_preserves_default(self):
  self.batch['purpose']='allocation_baseline';self.item['observation']=s.stock_observation(self.repo.path,'山东仓');data=v.candidate(self.receipt);j.commit_refresh(self.repo.path,j.prepare_refresh(self.repo.path,'山东仓',catalog=data))
  with self.assertRaises(wr.Conflict):self.ad.apply(self.batch,self.item)
  self.assertTrue(self.ad.catalogs.current_digest('山东仓').startswith('legacy:'))
  self.assertFalse((self.settings/'warehouse_catalogs/山东仓.json').exists())
  self.assertEqual(self.ad.catalogs.catalogs()['山东仓'],data)
 def test_allocation_refresh_idempotent(self):
  self.batch['purpose']='allocation_baseline';self.item['observation']=s.stock_observation(self.repo.path,'山东仓')
  ack=self.ad.apply(self.batch,self.item);before=s.stock_observation(self.repo.path,'山东仓')
  self.assertEqual(self.ad.apply(self.batch,self.item),ack);self.assertEqual(self.item['observation']['state'],before['state'])
  saved=self.ad.catalogs.catalogs()['山东仓']
  self.assertEqual(saved,v.candidate(self.receipt));self.assertEqual(j.catalogs(self.repo.path)['山东仓']['catalog'],saved)
  self.assertEqual(self.ad.catalogs.current_digest('山东仓'),self.ad.get(ack['apply_id'])['new_digest'])

 def test_invalid_stock_holds_adoption_only_for_affected_warehouse(self):
  self.ad.apply(self.batch,self.item);old=self.ad.catalogs.current_digest('山东仓')
  def candidate(warehouse,item_id,rows,columns=None):
   root=self.node/'downloads/next'/item_id;root.mkdir(parents=True)
   source=root/'stock.xlsx'
   book=openpyxl.Workbook();sheet=book.active;sheet.append(columns or wc.REQUIRED)
   for row in rows:sheet.append(row)
   book.save(source);book.close()
   receipt=v.validate({'schema':1,'warehouse':warehouse,'file':str(source),'sha256':v.digest(source),
                       'expected_rows':len(rows),'candidate':str(root/'candidate.json')})
   return {**self.item,'id':item_id,'account':{**self.item['account'],'warehouse':warehouse},
           'receipt':receipt,'native':{**self.item['native'],'sha256':receipt['source_sha256']},
           'expected_catalog_digest':self.ad.catalogs.current_digest(warehouse)}
  with self.assertRaisesRegex(ValueError,'缺少必需表头：可用数'):
   candidate('山东仓','missing-column',[['001','饮品',2,1,'启用']],wc.REQUIRED[:-1])
  batch={'id':'next','purpose':'default_catalog'}
  bad=candidate('山东仓','sd',[['001','饮品',2,1,'启用',None],['002','饮品',2,1,'备用','异常'],
                               ['003','饮品',2,1,'禁用',100]])
  with self.assertRaisesRegex(ValueError,'stock.xlsx：启用及备用商品的可用数全部'):
   self.ad.apply(batch,bad)
  analysis=self.ad.apply({**batch,'purpose':'stockout_analysis'},bad,publish_analysis=True)
  self.assertIn('可用数全部缺失',analysis['skipped_reason'])
  self.assertEqual(self.ad.catalogs.current_digest('山东仓'),old)
  valid=candidate('河北仓','hb',[['001','饮品',2,1,'启用',None],['002','饮品',2,1,'备用',0]])
  self.assertEqual(self.ad.apply(batch,valid)['warehouse'],'河北仓')
  data=self.ad.catalogs.catalogs()['河北仓']['items']
  self.assertIsNone(data['001']['可用数']);self.assertEqual(data['002']['可用数'],0)
  self.assertEqual(self.ad.catalogs.current_digest('山东仓'),old)

if __name__=='__main__':unittest.main()
