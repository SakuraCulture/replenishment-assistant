"""Final edits cross storage/sign/export boundaries, isolated generated database."""
import copy
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from test_preissue import row,result,edit,listing
import preissue as p
import work_repository as wr
import normal_orders as no
import normal_delivery as delivery
import normal_exports as ne
import history_forms
import result_semantics as sem
from version_info import MODEL_VERSION
from runtime_support import Cancelled


class ArchiveAdjustmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.work=wr.Repository(self.root/'settings');self.repo=no.Repository(self.work)
    def tearDown(self):self.tmp.cleanup()
    def payload(self,r):
        return {'schema':1,'day':wr.today(),'model_version':MODEL_VERSION,'input_dirty':False,'results':{'S1':r},'states':{'S1':{'state':'已完成','name':'合成店'}}}
    def adjusted(self,r,edits,lists=None):
        v=p.preview(r,edits,wr.today(),lists=lists);return p.finalize(v,[x['id'] for x in v['risks']])
    def test_roundtrip_signature_history_and_original_immutable(self):
        r=result();before=copy.deepcopy(r);a=self.adjusted(r,{'a':edit(0)});payload=self.payload(a)
        saved=self.work.save_job(wr.today(),payload);restored=self.work.get_job(wr.today())['payload'];self.assertEqual(restored,payload)
        p.validate_payload(restored,['S1']);orders=self.repo.confirm(restored,['S1'],saved['revision'],{'S1':None});order=orders[0]
        self.assertEqual(order['payload']['result'],a);self.assertEqual(p.original(a),before)
        form=history_forms.form(self.repo,order,'签发前调整','a');self.assertIn(['最终签发量','0','数字'],form)
        self.assertIn(['原自动安排量','24','数字'],form)
        self.assertEqual(ne.decision(a['rows'][0]),'人工不补');self.assertEqual(r,before)
    def test_pending_blocks_both_entry_points_no_archive(self):
        payload=self.payload(result());payload['preissue_drafts']={'S1':{'edits':{'a':edit(0)}}}
        self.work.save_job(wr.today(),payload)
        for fn in [lambda:delivery.prepare_batch(self.repo,payload,['S1']),lambda:self.repo.confirm(payload,['S1'],None,{'S1':None})]:
            with self.assertRaisesRegex(ValueError,'待核对'):fn()
        self.assertEqual(self.repo.headers(),[])
    def test_all_store_atomic_validation(self):
        a=self.adjusted(result(),{'a':edit(0)});b=copy.deepcopy(a);b['store']='S2'
        for d in b['rows']:d['B']='S2'
        payload=self.payload(a);payload['results']['S2']=b;payload['states']['S2']={'state':'已完成','name':'B'}
        with self.assertRaises(ValueError):self.repo.confirm(payload,['S1','S2'],None,{'S1':None,'S2':None})
        self.assertEqual(self.repo.headers(),[]);self.assertIsNone(self.work.get_job(wr.today()))
    def test_cancel_and_retry_unchanged(self):
        payload=self.payload(self.adjusted(result(),{'a':edit(0)}));stop=threading.Event();stop.set()
        with self.assertRaises(Cancelled):self.repo.confirm(payload,['S1'],None,{'S1':None},cancel=stop)
        self.assertEqual(self.repo.headers(),[]);stop.clear();self.repo.confirm(payload,['S1'],None,{'S1':None},cancel=stop)
        self.assertEqual(len(self.repo.headers()),1)
    def test_new_list_group_exactly_once_and_wholesale_qty(self):
        d=row('new',0,24);d.update(H=0,I=0,J=0,K=0,L=0,N=40);r=result([row(),d]);li=listing(['new'])
        filled=p.list_edits(r,li,'minimum');a=self.adjusted(r,filled['edits'],filled['lists']);order=self.repo.confirm(self.payload(a),['S1'],None,{'S1':None})[0]
        grouped=ne.groups(a,None);self.assertEqual([d['E'] for d in grouped['本月新品']],['new']);self.assertEqual([d['E'] for d in grouped['中心仓动销补货清单']],['a'])
        self.assertEqual(ne.decision(a['rows'][1]),'新品补足');path=self.root/'signed.xlsx';ne.export(order,path,warehouse='河北仓')
        import openpyxl
        with path.open('rb') as f:
            book=openpyxl.load_workbook(f);self.assertEqual(book.sheetnames,ne.SHEETS);self.assertEqual(book['本月新品']['Q2'].value,24);self.assertEqual(book['本月新品']['V2'].value,'新品补足');book.close()
        import order_lifecycle
        h={**order,'warehouse':'河北仓'};h.pop('payload')
        snapshot=order_lifecycle.snapshot(self.work.path,h)
        values={d[0]:d[2] for d in snapshot['rows']};self.assertEqual(values['new'],24)
    def test_unmodified_archived_old_revision_still_identical(self):
        r=result();first=self.repo.confirm(self.payload(r),['S1'],None,{'S1':None})[0]
        saved=self.work.get_job(wr.today());second=self.repo.confirm(self.payload(self.adjusted(r,{'a':edit(0)})),['S1'],saved['revision'],{'S1':1})[0]
        self.assertEqual(second['revision'],2);self.assertEqual(self.repo.get(wr.today(),'S1',1),{**first,'current_revision':2})


if __name__=='__main__':unittest.main()
