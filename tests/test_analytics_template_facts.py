"""Template fields use retained signing/input evidence, with unknowns intact."""
from contextlib import closing
from decimal import Decimal as D
import hashlib
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import patch

sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'src'),str(Path(__file__).parent)]
import openpyxl
import normal_orders as orders
import order_analytics as analytics
import source_archive
import test_order_analytics as fixture


class TemplateFacts(unittest.TestCase):
    def setUp(self):
        self.f=fixture.AnalyticsTests();self.f.setUp();self.addCleanup(self.f.tearDown)
        self.backup=patch('storage_chunks.backup_before_write');self.backup.start();self.addCleanup(self.backup.stop)
        self.raw=self.f.p['result']['rows'][0]

    def persist_catalogs(self):
        with closing(sqlite3.connect(self.f.path)) as db,db:
            db.execute('UPDATE normal_work_revisions SET payload=?',
                       (orders.encode({'catalogs':self.f.catalogs},db),))

    def archive_stock(self,headers=None,rows=None,frozen=None):
        headers=headers or ['商品编码','商品状态','虚拟分类','可用数']
        rows=rows if rows is not None else [['0001','启用','',240]]
        book=openpyxl.Workbook();sheet=book.active;sheet.title='原资料';sheet.append(headers)
        for row in rows:sheet.append(row)
        path=self.f.root/'template-frozen-stock.xlsx';book.save(path);book.close()
        sha=hashlib.sha256(path.read_bytes()).hexdigest();source_archive.capture(self.f.path,path,sha)
        self.f.catalogs['河北仓']={'source_sha256':sha,'sheet':'原资料','revision':'frozen-stock-v1',
            'imported_at':'2026-11-01T08:30:00','items':frozen or {}}
        self.persist_catalogs();return path

    def first(self):return self.f.report()['rows'][0]

    def test_all_fields_project_frozen_values_without_changing_funnel_metrics(self):
        baseline=self.f.report()['metrics']
        self.raw.update(F='0000000123456',S='启用',K=4,J=60,L=0,BF=148,AP=140,AL=8,BD=24,BQ=999,
                        _mandatory_holding={'minimum':24})
        self.raw['_meta']['export_updated']='2026-11-06 10:25:00';self.f.save_payload()
        file=self.archive_stock(rows=[['0001','启用','',0]],frozen={'0001':{'可用数':0}})
        file.unlink()  # The captured original, not its old filesystem path, is authoritative.
        before=self.f.path.read_bytes()
        with patch('warehouse_catalog.Repository.catalogs',side_effect=AssertionError('current catalog read')):
            report=self.f.report()
        values=report['rows'][0]['template_attributes']
        expected={'barcode':'0000000123456','supply_state':'启用','stock':D(4),'monthly_sales':D(60),
            'in_transit':D(0),'target_holding':D(148),'pack_size':D(24),'minimum_holding':D(24),
            'stock_snapshot_at':'2026-11-06 10:25:00','warehouse_stock':D(0)}
        self.assertEqual({key:values[key] for key in expected},expected)
        self.assertEqual(values['warehouse_stock_basis']['sources'],['冻结仓资料','归档原件'])
        self.assertEqual(values['warehouse_stock_basis']['catalog_imported_at'],'2026-11-01T08:30:00')
        self.assertEqual(report['metrics'],baseline);self.assertEqual(self.f.path.read_bytes(),before)

    def test_missing_input_fields_do_not_use_recovery_quantity_or_signing_time(self):
        for key in ('F','S','K','J','L','BF','AP','AL','BD','_mandatory_holding'):self.raw.pop(key,None)
        self.raw['BQ']=999;self.raw['_meta'].pop('export_updated',None);self.f.save_payload()
        values=self.first()['template_attributes']
        self.assertTrue(all(values[key] is None for key in ('barcode','supply_state','stock','monthly_sales',
            'in_transit','target_holding','pack_size','minimum_holding','stock_snapshot_at','warehouse_stock')))

    def test_target_prefers_frozen_quantile_then_frozen_forecast_plus_safety(self):
        self.raw.update(BF=0,AP=7,AL=2);self.f.save_payload()
        self.assertEqual(self.first()['template_attributes']['target_holding'],0)
        self.raw.pop('BF');self.f.save_payload()
        self.assertEqual(self.first()['template_attributes']['target_holding'],9)
        self.raw['AL']='待核';self.f.save_payload()
        self.assertIsNone(self.first()['template_attributes']['target_holding'])

    def test_invalid_numeric_fields_stay_unknown_and_explicit_zero_survives(self):
        self.raw.update(F=1234567890123456,K='NaN',J=True,L='Infinity',BF='待核',AP=None,AL=8,
                        BD='待核箱规',_mandatory_holding={'minimum':'待核'})
        self.f.save_payload();values=self.first()['template_attributes']
        for key in ('barcode','stock','monthly_sales','in_transit','target_holding','pack_size','minimum_holding'):
            self.assertIsNone(values[key],key)
        self.raw.update(K=0,J=0,L=0,BD=1,_mandatory_holding={'minimum':0});self.f.save_payload()
        values=self.first()['template_attributes']
        for key in ('stock','monthly_sales','in_transit','minimum_holding'):self.assertEqual(values[key],0,key)

    def test_original_stock_requires_unique_code_and_column_and_valid_value(self):
        cases=[
            (['商品编码','商品状态','虚拟分类'],[['0001','启用','']]),
            (['商品编码','商品状态','虚拟分类','可用数'],[['0001','启用','','待核']]),
            (['商品编码','商品状态','虚拟分类','可用数'],[['0001','启用','',2],['0001','启用','',2]]),
            (['商品编码','商品状态','虚拟分类','可用数','可用数'],[['0001','启用','',2,2]]),
            (['商品编码','商品编码','商品状态','虚拟分类','可用数'],[['0001','0001','启用','',2]]),
        ]
        for headers,rows in cases:
            with self.subTest(headers=headers,rows=rows):
                self.archive_stock(headers,rows)
                self.assertIsNone(self.first()['template_attributes']['warehouse_stock'])

    def test_frozen_stock_survives_missing_original_but_conflicting_evidence_is_unknown(self):
        frozen={'0001':{'商品编码':'0001','商品状态':'启用','虚拟分类':'','可用数':0}}
        self.archive_stock(rows=[['0001','启用','',0]],frozen=frozen)
        with closing(sqlite3.connect(self.f.path)) as db,db:db.execute('DELETE FROM source_files')
        values=self.first()['template_attributes']
        self.assertEqual(values['warehouse_stock'],0);self.assertEqual(values['warehouse_stock_basis']['sources'],['冻结仓资料'])
        self.archive_stock(rows=[['0001','启用','',240]],frozen=frozen)
        values=self.first()['template_attributes'];self.assertIsNone(values['warehouse_stock'])
        self.assertIn('冲突','；'.join(values['warehouse_stock_basis']['notes']))

    def test_duplicate_original_does_not_get_repaired_by_frozen_stock(self):
        self.archive_stock(rows=[['0001','启用','',2],['0001','启用','',2]],
                           frozen={'0001':{'商品状态':'启用','虚拟分类':'','可用数':2}})
        self.assertIsNone(self.first()['template_attributes']['warehouse_stock'])

    def test_added_sku_does_not_inherit_signed_store_inputs(self):
        self.archive_stock(rows=[['0001','启用','',240],['EXTRA','启用','',7]])
        self.f.f.save('return',[{'商品编码':'EXTRA','门店确认数量':3}])
        extra=next(row for row in self.f.report()['rows'] if row['sku']=='EXTRA')
        self.assertEqual(extra['scope'],'待核');values=extra['template_attributes']
        self.assertEqual(values['warehouse_stock'],7)
        for key in ('barcode','stock','monthly_sales','in_transit','target_holding','pack_size','minimum_holding','stock_snapshot_at'):
            self.assertIsNone(values[key],key)

    def test_new_attributes_are_covered_by_existing_snapshot_validation(self):
        self.archive_stock();report=self.f.report()
        self.f.catalogs['河北仓']['items']={'0001':{'可用数':1}};self.persist_catalogs()
        current=self.f.report();self.assertNotEqual(current['snapshot_id'],report['snapshot_id'])
        with self.assertRaisesRegex(ValueError,'变化'):analytics.validate_snapshot(self.f.path,report)

    def test_order_status_uses_lifecycle_view_including_zero_demand_completion(self):
        self.f.f.save('return',[{'门店确认数量':0}]);view=self.f.f.view();row=self.first()
        self.assertEqual(view['status'],'已收齐');self.assertIsNone(view['next'])
        self.assertEqual(row['order_status'],view['status']);self.assertEqual(row['order_next'],view['next'])


if __name__=='__main__':unittest.main()
