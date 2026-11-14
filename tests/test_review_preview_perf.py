"""Full signing review evidence keeps values, order and prepare-time validation."""
import copy
import datetime
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path

import normal_orders
import order_review
import work_repository as wr
from review_json import Prepared
from runtime_support import Cancelled
from test_preissue import result, row
from ui_work_io import Session
from version_info import MODEL_VERSION


class ReviewJsonTests(unittest.TestCase):
    def test_same_bytes_as_original_encoders(self):
        tree={'中文':[0,0.0,False,None,{},[],{'z':'首行\n次行','a':1.23456789}], 'order':{'b':2,'a':1}}
        for canonical in (False,True):
            value=copy.deepcopy(tree)
            if canonical:value['date']=datetime.date(2026,11,13)
            expected=wr.dumps(value) if canonical else json.dumps(value,ensure_ascii=False,indent=2)
            prepared=Prepared(value,canonical=canonical)
            for _ in range(2):
                output=io.StringIO();prepared.write(output)
                self.assertEqual(output.getvalue().encode('utf-8'),expected.encode('utf-8'))

    def test_unsupported_and_nonfinite_fail_during_preparation(self):
        with self.assertRaises(TypeError):Prepared({'date':datetime.date(2026,11,13)})
        for v in (float('nan'),float('inf'),-float('inf')):
            with self.subTest(value=v),self.assertRaises(ValueError):Prepared({'x':v},canonical=True)
        with self.assertRaises(TypeError):Prepared({'x':object()},canonical=True)
        cycle=[];cycle.append(cycle)
        with self.assertRaises(ValueError):Prepared(cycle)

    def test_preparation_remains_cancellable(self):
        stop=threading.Event();stop.set()
        with self.assertRaises(Cancelled):Prepared({'x':list(range(1000))},cancel=stop)


class ReviewSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.work=wr.Repository(self.root/'settings');self.repo=normal_orders.Repository(self.work)
        r=result([row('001'),row('中文')]);r['effective_settings']={'b':False,'a':0.0,'n':None}
        self.payload=dict(schema=1,day=wr.today(),model_version=MODEL_VERSION,input_dirty=False,
                          results={'S1':r},states={'S1':dict(state='已完成',name='合成店')})
    def tearDown(self):self.tmp.cleanup()

    def test_full_json_and_spreadsheet_preserve_diff_beyond_preview(self):
        old=self.repo.confirm(self.payload,['S1'],None,{'S1':None})[0]
        changed=copy.deepcopy(self.payload);r=changed['results']['S1']
        r['rows']=[row(f'{i:05}',48,24) for i in range(150)]
        import result_semantics as sem
        r['summary']=sem.summarize(r['rows'])
        review=order_review.build(old,r,None)
        with Session(self.work.path) as session:
            preview=session.call('prepare_normal',[changed,['S1']],lambda _:None)
            self.assertEqual({k:preview[k] for k in order_review.preview([review])},order_review.preview([review]))
            self.assertTrue(preview['truncated']);self.assertGreater(preview['total'],200)
            for name in ('full.json','repeat.json','full.xlsx'):
                session.call('review_export',str(self.root/name),lambda _:None)
        details=json.loads((self.root/'full.json').read_text('utf-8'))
        self.assertEqual(details['S1']['changes'],normal_orders.revision_diff(old['payload']['result'],r))
        self.assertEqual((self.root/'full.json').read_bytes(),(self.root/'repeat.json').read_bytes())
        import openpyxl
        book=openpyxl.load_workbook(self.root/'full.xlsx',read_only=True)
        actual=list(book['签发差异'].values);book.close()
        # Excel empty strings read back as None; preserve the complete row order.
        expected=[order_review.HEADERS,*list(order_review.rows([review]))]
        self.assertEqual(actual,[tuple(None if v=='' else v for v in row) for row in expected])
        self.assertEqual(self.repo.head(wr.today(),'S1')['revision'],1)

    def test_delivery_review_keeps_canonical_json(self):
        review=order_review.build(None,self.payload['results']['S1'],None)
        with Session(self.work.path) as session:
            session.call('delivery_prepare',[self.payload,['S1']],lambda _:None)
            session.call('review_export',str(self.root/'delivery.json'),lambda _:None)
        self.assertEqual((self.root/'delivery.json').read_text('utf-8'),wr.dumps([review]))
        self.assertEqual(self.repo.headers(),[])

    def test_failed_new_prepare_cannot_export_previous_review(self):
        with Session(self.work.path) as session:
            session.call('prepare_normal',[self.payload,['S1']],lambda _:None)
            invalid=copy.deepcopy(self.payload);invalid['results']['S1']['effective_settings']['unknown']=datetime.date(2026,11,13)
            with self.assertRaisesRegex(RuntimeError,'serializable'):
                session.call('prepare_normal',[invalid,['S1']],lambda _:None)
            with self.assertRaisesRegex(RuntimeError,'尚未生成完整差异'):
                session.call('review_export',str(self.root/'stale.json'),lambda _:None)
        self.assertFalse((self.root/'stale.json').exists());self.assertEqual(self.repo.headers(),[])

    def test_existing_json_is_never_replaced_and_session_can_retry(self):
        target=self.root/'exists.json';target.write_bytes(b'original')
        with Session(self.work.path) as session:
            session.call('prepare_normal',[self.payload,['S1']],lambda _:None)
            with self.assertRaisesRegex(RuntimeError,'尚不存在'):
                session.call('review_export',str(target),lambda _:None)
            session.call('review_export',str(self.root/'new.json'),lambda _:None)
        self.assertEqual(target.read_bytes(),b'original');self.assertEqual(self.repo.headers(),[])


if __name__=='__main__':unittest.main()
