"""Actual export-header compatibility and automatic monthly first-fill pipeline."""
import copy
import hashlib
import tempfile
import threading
import unittest
from pathlib import Path

import openpyxl
import warehouse_catalog as wc
import first_fill
import batch_service
import engine
import new_product_focus as focus
import new_products
import normal_exports
import priority_list
import result_semantics as sem
import restock_policy
from runtime_support import Cancelled
from table_records import bridge_table


def monthly(month='2026-11',codes=('0001',)):
    value=focus.empty(month);value['items']=[{'sku':s,'focused':True,'name':'合成新品'} for s in codes];value['revision']=focus.revision(value);return value


class MonthlyFirstFill(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name);self.serial=0
    def load(self,fields=('新店新品首铺数量',),values=(18,),*,status='启用',tag='',sales=None):
        self.serial+=1;catalogs={}
        for wh,stock in [('山东仓',0),('河北仓',100)]:
            path=self.root/f'{self.serial}-{wh}.xlsx';book=openpyxl.Workbook();sheet=book.active
            sheet.append(wc.REQUIRED+['商品名称','商品标签']+list(fields))
            quantities=list(values) if wh=='山东仓' else [30]*len(fields)
            sheet.append(['0001','饮品',2,6,status,stock,'合成新品',tag]+quantities);book.save(path);book.close()
            before=hashlib.sha256(path.read_bytes()).hexdigest();catalogs[wh]=wc.import_catalog(path,wh)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),before)
        row={'仓库/门店编码':'S1','仓库/门店名称':'合成店','商品编码':'0001','商品名称':'合成新品','日销':0,'周销':0,'月销':0,'可用库存':0,'在途库存':0,'缺货天数（天）':0,'补货状态':'正常'}
        row.update(sales or {})
        loaded=wc.prepare_records(bridge_table(list(row),[row],'销量原表'),catalogs)
        return loaded,catalogs
    def calculate(self,loaded,*,primary='山东仓',secondary=None,budget=None,monthly_profile='default',new_enabled=True):
        settings={'warehouses':copy.deepcopy(engine.DEFAULT_WAREHOUSES),'exception':False,'budget':budget,'supply_route':{'primary':primary,'secondary':secondary}}
        profile=monthly() if monthly_profile=='default' else monthly_profile
        settings=new_products.calculation_settings(settings,profile,'2026-11-12')
        loaded=copy.deepcopy(loaded)
        if not new_enabled:loaded['config']=restock_policy.patch(loaded['config'],{'new_stocking':False})
        priority=priority_list.make_profile([{'sku':'9999','name':'未出现必采'}],'隔离必采清单') if budget is not None else None
        return batch_service.calculate_store(loaded,'S1',settings,priority)

    def test_actual_and_legacy_column_names_import_and_auto_fill(self):
        for field in first_fill.FIELDS:
            with self.subTest(field=field):
                loaded,catalogs=self.load((field,));r=self.calculate(loaded);row=r['rows'][0]
                self.assertEqual(catalogs['山东仓']['items']['0001'][first_fill.FIELD],18)
                self.assertEqual(row['_meta']['first_fill']['source_field'],field)
                self.assertEqual(row['AS'],0);self.assertEqual(sem.order_quantity(row),18)
                self.assertIn(field,sem.explanation(row));self.assertIn('自动首铺',sem.explanation(row));self.assertNotIn('人工选择',sem.explanation(row))

    def test_duplicate_or_conflicting_synonyms_use_minimum_without_choosing(self):
        for fields in (first_fill.FIELDS,('新店新品首铺数量',)*2,(first_fill.FIELD,)*2):
            with self.subTest(fields=fields):
                loaded,catalogs=self.load(fields,(18,24));row=self.calculate(loaded)['rows'][0]
                self.assertNotIn(first_fill.FIELD,catalogs['山东仓']['items']['0001'])
                self.assertEqual(sem.order_quantity(row),6);self.assertIn('未自动择一',sem.explanation(row))

    def test_missing_blank_invalid_values_fall_back_to_outbound_step(self):
        loaded,_=self.load((),());self.assertEqual(sem.order_quantity(self.calculate(loaded)['rows'][0]),6)
        for value in (None,'',0,-1,'错误',True):
            with self.subTest(value=value):
                loaded,_=self.load(values=(value,));row=self.calculate(loaded)['rows'][0]
                self.assertEqual(sem.order_quantity(row),6);self.assertEqual(row['_meta']['first_fill']['raw'],None if value=='' else value)
    def test_quantity_is_rounded_up_to_carton_and_raw_value_remains(self):
        loaded,_=self.load(values=(19,));row=self.calculate(loaded)['rows'][0]
        self.assertEqual(sem.order_quantity(row),24);self.assertEqual(row['_meta']['first_fill']['raw'],19)
    def test_assigned_secondary_warehouse_supplies_its_own_quantity(self):
        loaded,_=self.load();row=self.calculate(loaded,secondary='河北仓')['rows'][0]
        self.assertEqual(row['AU'],'河北仓');self.assertEqual(sem.order_quantity(row),30)
        self.assertEqual(row['_meta']['first_fill']['warehouse'],'河北仓')
    def test_nonmonthly_and_no_focus_do_not_automatically_fill(self):
        loaded,_=self.load()
        for profile in (None,monthly(codes=()),monthly('2026-10')):
            with self.subTest(profile=profile):
                r=self.calculate(loaded,monthly_profile=profile);self.assertEqual(sem.order_quantity(r['rows'][0]),0);self.assertNotIn('new_products',r)
    def test_store_switch_off_retains_zero_and_candidates(self):
        loaded,_=self.load();r=self.calculate(loaded,new_enabled=False)
        self.assertEqual(sem.order_quantity(r['rows'][0]),0);self.assertTrue(new_products.is_new(r['rows'][0]));self.assertNotIn('new_products',r)
    def test_disabled_daily_and_store_stop_are_excluded_before_first_fill(self):
        for kwargs in ({'status':'禁用'},{'tag':'日配'},{'sales':{'补货状态':'停采'}}):
            with self.subTest(kwargs=kwargs):
                loaded,_=self.load(**kwargs);r=self.calculate(loaded);self.assertEqual(sem.order_quantity(r['rows'][0]),0)
                self.assertNotIn('new_products',r)
    def test_inbound_stock_negative_missing_or_conflicting_sales_are_not_new(self):
        for sales in ({'在途库存':6},{'可用库存':2},{'可用库存':-1},{'可用库存':None},{'月销':1},{'日销':1}):
            with self.subTest(sales=sales):
                loaded,_=self.load(sales=sales);r=self.calculate(loaded)
                self.assertNotIn('new_products',r);self.assertNotIn('_new_requested',r['rows'][0])
    def test_budget_can_limit_auto_quantity_and_reason_explains_zero(self):
        loaded,_=self.load()
        for budget,expected in ((24,12),(0,0)):
            with self.subTest(budget=budget):
                r=self.calculate(loaded,budget=budget);row=r['rows'][0]
                self.assertEqual(row['_new_requested'],18);self.assertEqual(sem.order_quantity(row),expected)
                self.assertIn('预算',sem.explanation(row));self.assertLessEqual(r['summary']['amount'],budget*1.05)
    def test_compact_undo_restores_model_and_budget_without_retaining_full_base(self):
        loaded,_=self.load();auto=self.calculate(loaded,budget=24);before=copy.deepcopy(auto)
        base=new_products.original(auto);plain=self.calculate(loaded,budget=24,monthly_profile=None)
        self.assertEqual(base['rows'],plain['rows']);self.assertEqual(base['budget'],plain['budget']);self.assertEqual(base['summary'],plain['summary'])
        self.assertEqual(auto,before);self.assertNotIn('new_products',base)
        self.assertNotIn('new_product_bases',auto);self.assertNotIn('rows',auto['rows'][0]['_monthly_first_fill'])
    def test_manual_change_or_deselection_is_preserved_until_explicit_recalculation(self):
        loaded,_=self.load();auto=self.calculate(loaded);base=new_products.original(auto)
        manual=new_products.apply(base,{'0001':12});self.assertEqual(sem.order_quantity(manual['rows'][0]),12)
        self.assertFalse(manual['new_products'].get('automatic'));self.assertNotIn('_monthly_first_fill',manual['rows'][0])
        empty=new_products.apply(base,{});self.assertEqual(sem.order_quantity(empty['rows'][0]),0)
    def test_normal_sales_replenishment_does_not_change(self):
        loaded,_=self.load(sales={'日销':3,'周销':21,'月销':90})
        auto=self.calculate(loaded);plain=self.calculate(loaded,monthly_profile=None)
        self.assertEqual(auto['rows'],plain['rows']);self.assertEqual(auto['summary'],plain['summary'])
    def test_frozen_settings_do_not_change_when_live_focus_changes(self):
        profile=monthly();settings=new_products.calculation_settings({'budget':None},profile,'2026-11-12')
        profile['items'].clear();self.assertEqual(settings['monthly_first_fill']['focus']['items'][0]['sku'],'0001')
    def test_store_policy_off_then_on_recomputes_only_eligible_monthly_candidates(self):
        import parameter_schema,restock_trial
        loaded,_=self.load();current=self.calculate(loaded);cfg=parameter_schema.standard();current['config']=cfg
        settings={'parameter_config':cfg,'budget':None,'monthly_first_fill':{'day':'2026-11-12','focus':monthly()}}
        original=copy.deepcopy(current);off=restock_policy.patch(cfg,{'new_stocking':False})
        preview=restock_trial.prepare(loaded,{'S1':settings},{'S1':current},{},{},{'S1':off},None,'2026-11-12')
        disabled=restock_trial.finalize(preview,{'S1':current})['S1'];self.assertEqual(sem.order_quantity(disabled['rows'][0]),0);self.assertEqual(current,original)
        on=restock_policy.patch(off,{'new_stocking':True})
        preview=restock_trial.prepare(loaded,{'S1':settings},{'S1':disabled},{},{},{'S1':on},None,'2026-11-12')
        restored=restock_trial.finalize(preview,{'S1':disabled})['S1'];self.assertEqual(sem.order_quantity(restored['rows'][0]),18)
    def test_saved_auto_result_can_rebuild_manual_base_after_json_roundtrip(self):
        import json
        loaded,_=self.load();auto=self.calculate(loaded);restored=json.loads(json.dumps(auto))
        changed=new_products.apply(new_products.original(restored),{'0001':12})
        self.assertEqual(sem.order_quantity(changed['rows'][0]),12);self.assertEqual(sem.order_quantity(auto['rows'][0]),18)
    def test_original_requires_complete_compact_evidence(self):
        loaded,_=self.load();r=self.calculate(loaded);del r['rows'][0]['_monthly_first_fill']['original_fields']['_candidate_qty']
        with self.assertRaisesRegex(ValueError,'原始依据不完整'):new_products.original(r)
    def test_cancellation_stops_without_publishing_mutated_source(self):
        loaded,_=self.load();before=copy.deepcopy(loaded);stop=threading.Event();stop.set()
        settings=new_products.calculation_settings({'warehouses':engine.DEFAULT_WAREHOUSES,'exception':False,'budget':None,'supply_route':{'primary':'山东仓','secondary':None}},monthly(),'2026-11-12')
        with self.assertRaises(Cancelled):batch_service.calculate_store(loaded,'S1',settings,None,cancel=stop)
        self.assertEqual(loaded,before)
    def test_actual_confirmation_export_contains_auto_quantity_without_manual_selection(self):
        loaded,_=self.load();r=self.calculate(loaded);out=self.root/'confirm.xlsx'
        order={'number':'BH-S1-ZC','store':'S1','digest':'synthetic','day':'2026-11-12','revision':1,'current_revision':1,'payload':{'result':r,'new_focus':monthly()}}
        normal_exports.export(order,out);book=openpyxl.load_workbook(out,data_only=True)
        try:
            sheet=book['本月新品'];self.assertEqual(sheet.max_row,2);self.assertEqual(sheet['Q2'].value,18)
        finally:book.close()


if __name__=='__main__':unittest.main()
