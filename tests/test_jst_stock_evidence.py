"""Exact frozen stock provenance exports; every database/file is synthetic."""
import copy
import sqlite3
import sys
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import openpyxl

sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'src')]
import jst_stock_evidence as evidence
import jst_batches as batches
import jst_orders as orders
import warehouse_catalog
import work_repository as wr
from runtime_support import Cancelled
import test_jst as fixture


class StockEvidence(unittest.TestCase):
    def setUp(self):
        self.f=fixture.Service();self.f.setUp();self.addCleanup(self.f.tearDown)
        self.path=self.f.path;self.root=self.f.f.root;self.h=self.f.h

    def output(self,name='核对.xlsx'):return self.root/name

    def tables(self,exclude=()):
        with closing(sqlite3.connect(self.path)) as db:
            names=[r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            return {n:db.execute('SELECT * FROM "'+n+'"').fetchall() for n in names if n not in exclude}

    def sheets(self,path):
        book=openpyxl.load_workbook(path,read_only=True,data_only=False)
        try:
            return {sheet.title:[dict(zip(next(iter(sheet.values)),values)) for values in list(sheet.values)[1:]] for sheet in book}
        finally:book.close()

    def item_rows(self,values):return [row for name,rows in values.items() if name.startswith('采用字段_') for row in rows]

    def new_batch(self,heads=None,stock=100,catalog=None):
        return batches.preview_new(self.path,heads or [self.h],{'河北仓':catalog or fixture.catalog(stock=stock)})

    def test_generation_keeps_original_100_after_global_stock_refreshes_to_900(self):
        _,gid=self.f.generated();old=orders.get(self.path,gid);self.f.refresh(900)
        before=self.tables()
        with patch.object(warehouse_catalog.Repository,'catalogs',side_effect=AssertionError('must not read current defaults')):
            path=evidence.export_generations(self.path,[gid],self.output())
        values=self.sheets(path);summary=values['资料批次'][0];row=values['分配核对'][0]
        self.assertEqual(summary['库存依据ID'],old['bid'])
        self.assertEqual(self.item_rows(values)[0]['可用数'],100)
        self.assertEqual((row['资料可用'],row['分配前余额'],row['实际分配量'],row['分配后余额']),(100,100,40,60))
        self.assertEqual(self.tables(),before)

    def test_registered_batch_exports_full_stock_before_members_are_generated(self):
        other=self.f.second_store();plan=self.new_batch([self.h,other],stock=140)
        batches.register(self.path,plan);before=self.tables()
        values=self.sheets(evidence.export_batch(self.path,plan['payload']['batch_id'],self.output()))
        self.assertEqual(values['资料批次'][0]['状态'],'已冻结')
        self.assertEqual(self.item_rows(values)[0]['可用数'],140)
        self.assertEqual(values['分配核对'],[])
        self.assertIn('尚未生成',values['资料批次'][0]['导出范围'])
        self.assertEqual(self.tables(),before)

    def test_unconfirmed_preview_exports_chosen_300_and_does_not_register(self):
        plan=self.new_batch(stock=300);before=self.tables()
        values=self.sheets(evidence.export_preview(self.path,plan,self.output()))
        self.assertEqual(values['资料批次'][0]['状态'],'未确认预览')
        self.assertEqual(self.item_rows(values)[0]['可用数'],300)
        self.assertEqual(values['分配核对'][0]['资料可用'],300)
        self.assertEqual(batches.listing(self.path),[]);self.assertEqual(self.tables(),before)

    def test_saved_batch_preview_keeps_its_stock_after_global_refresh(self):
        plan=self.new_batch(stock=120);batches.register(self.path,plan);self.f.refresh(900)
        continued=batches.preview_existing(self.path,plan['payload']['batch_id'])
        values=self.sheets(evidence.export_preview(self.path,continued,self.output()))
        self.assertEqual(self.item_rows(values)[0]['可用数'],120)
        self.assertIn('未确认',values['资料批次'][0]['状态'])

    def test_two_batches_same_warehouse_preserve_separate_stock_versions(self):
        first=batches.execute(self.path,self.new_batch(stock=100));other=self.f.second_store()
        second=batches.execute(self.path,self.new_batch([other],stock=70))
        values=self.sheets(evidence.export_generations(self.path,[*first['generated'],*second['generated']],self.output()))
        self.assertEqual(len(values['资料批次']),2)
        self.assertEqual(len({r['库存依据ID'] for r in values['资料批次']}),2)
        self.assertEqual([r['可用数'] for r in self.item_rows(values)],[100,70])
        self.assertEqual(sum(name.startswith('采用字段_河北仓') for name in values),2)

    def test_batch_export_excludes_replaced_generations_but_exact_history_is_labelled(self):
        first=batches.execute(self.path,self.new_batch(stock=100));bid=first['batch_id'];old=first['generated'][0]
        replacement=batches.execute(self.path,batches.preview_existing(self.path,bid,[self.h],True))
        self.assertFalse(replacement['errors']);new=replacement['generated'][0];self.assertNotEqual(new,old)
        values=self.sheets(evidence.export_batch(self.path,bid,self.output('本批当前.xlsx')))
        self.assertEqual({r['生成记录ID'] for r in values['分配核对']},{new})
        values=self.sheets(evidence.export_generations(self.path,[old],self.output('历史原件.xlsx')))
        self.assertEqual({r['生成记录ID'] for r in values['分配核对']},{old})
        self.assertTrue(any('历史记录' in str(value) for row in values['分配核对'] for value in row.values()))

    def test_missing_old_baseline_never_falls_back_to_the_current_900(self):
        _,gid=self.f.generated();old=orders.get(self.path,gid);self.f.refresh(900)
        with closing(sqlite3.connect(self.path)) as db,db:db.execute('DELETE FROM jst_baselines WHERE bid=?',(old['bid'],))
        before=self.tables()
        with self.assertRaisesRegex(ValueError,'原库存依据不存在'):
            evidence.export_generations(self.path,[gid],self.output())
        self.assertFalse(self.output().exists());self.assertEqual(self.tables(),before)

    def test_bad_frozen_baseline_digest_is_rejected_without_output(self):
        _,gid=self.f.generated();old=orders.get(self.path,gid)
        with closing(sqlite3.connect(self.path)) as db,db:db.execute('UPDATE jst_baselines SET digest=? WHERE bid=?',('broken',old['bid']))
        before=self.tables()
        with self.assertRaisesRegex(ValueError,'快照校验失败'):
            evidence.export_generations(self.path,[gid],self.output())
        self.assertFalse(self.output().exists());self.assertEqual(self.tables(),before)

    def test_tampered_preview_cannot_be_exported(self):
        plan=self.new_batch();plan['payload']['plans'][0]['rows'][0]['stock']='999'
        with self.assertRaisesRegex(ValueError,'预览已变化'):
            evidence.export_preview(self.path,plan,self.output())
        self.assertFalse(self.output().exists())

    def test_long_codes_formula_looking_names_and_unknown_values_are_preserved(self):
        catalog=fixture.catalog();sku='0012345678901234567890123456789'
        extra=copy.deepcopy(catalog['items']['0001']);extra.update(商品编码=sku,商品名称='=1+1',可用数=None,商品标签=['日配','自定义'])
        catalog['items'][sku]=extra;catalog['revision']=warehouse_catalog.revision(catalog['items'])
        target=evidence.export_preview(self.path,self.new_batch(catalog=catalog),self.output())
        book=openpyxl.load_workbook(target,read_only=True,data_only=False)
        try:
            sheet=next(s for s in book if s.title.startswith('采用字段_'))
            rows=list(sheet.rows);headers=[c.value for c in rows[0]];row=rows[2]
            self.assertEqual(row[headers.index('商品编码')].value,sku)
            self.assertEqual(row[headers.index('商品编码')].data_type,'s')
            self.assertEqual(row[headers.index('商品名称')].value,'=1+1')
            self.assertEqual(row[headers.index('商品名称')].data_type,'s')
            self.assertIsNone(row[headers.index('可用数')].value)
            self.assertEqual(row[headers.index('商品标签')].value,'["日配", "自定义"]')
        finally:book.close()

    def test_export_does_not_reopen_source_path_or_claim_platform_freshness(self):
        _,gid=self.f.generated();before=self.tables()
        with patch.object(Path,'read_bytes',side_effect=AssertionError('must not read a current file')):
            target=evidence.export_generations(self.path,[gid],self.output())
        values=self.sheets(target);row=values['资料批次'][0]
        self.assertEqual(row['平台导出时间'],'未留存')
        self.assertIn('未留存',row['原件归档记录'])
        self.assertIn('不等于平台库存时点',row['说明'])
        self.assertEqual(self.tables(),before)

    def test_existing_file_is_not_overwritten(self):
        _,gid=self.f.generated();target=self.output();target.write_bytes(b'KEEP ORIGINAL')
        before=self.tables()
        with self.assertRaises(FileExistsError):evidence.export_generations(self.path,[gid],target)
        self.assertEqual(target.read_bytes(),b'KEEP ORIGINAL');self.assertEqual(self.tables(),before)

    def test_cancellation_before_and_during_writing_leaves_no_result_or_temp_file(self):
        _,gid=self.f.generated();before=self.tables();stop=threading.Event();stop.set()
        with self.assertRaises(Cancelled):evidence.export_generations(self.path,[gid],self.output(),cancel=stop)
        stop.clear()
        def progress(_):stop.set()
        with self.assertRaises(Cancelled):evidence.export_generations(self.path,[gid],self.output(),progress=progress,cancel=stop)
        self.assertFalse(self.output().exists());self.assertEqual(list(self.root.glob('.核对.*.tmp.xlsx')),[])
        self.assertEqual(self.tables(),before)

    def test_excel_writing_does_not_hold_a_database_read_transaction(self):
        _,gid=self.f.generated()
        with closing(sqlite3.connect(self.path)) as db,db:db.execute('CREATE TABLE qa_probe(value TEXT)')
        before=self.tables(exclude=('qa_probe',));wrote=[]
        def progress(_):
            if wrote:return
            with closing(sqlite3.connect(self.path,timeout=.1)) as db,db:
                db.execute('BEGIN IMMEDIATE');db.execute('INSERT INTO qa_probe VALUES(?)',('writer completed during Excel output',));db.commit()
            wrote.append(True)
        evidence.export_generations(self.path,[gid],self.output(),progress=progress)
        self.assertEqual(wrote,[True]);self.assertEqual(self.tables(exclude=('qa_probe',)),before)
        with closing(sqlite3.connect(self.path)) as db:self.assertEqual(db.execute('SELECT count(*) FROM qa_probe').fetchone()[0],1)


if __name__=='__main__':unittest.main()
