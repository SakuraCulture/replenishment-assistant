"""Business scenarios for default must-sell stock and daily-delivery exclusion.

All goods and databases are synthetic. Expected quantities are hand calculated;
the forecast is compared separately from the new execution floor.
"""
import copy
from contextlib import closing
from decimal import Decimal
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'src'),str(Path(__file__).parent)]
import admission
import engine
import mandatory_holding as floor
import new_products
import preissue
import priority_list
import result_semantics as sem
from runtime_support import Cancelled
from test_rules import row


def loaded(*changes,minimum=1):
    normalized=[]
    for i,fields in enumerate(changes or ({},)):
        d=row(H=0,I=0,J=0,K=0,L=0,N=0,O=1,V=2,B='T',E=str(i+1))
        d.update(_source='资料匹配',AU='河北仓',U='快销',AT='无有效拒收记录')
        d.update({k:v for k,v in fields.items() if not k.startswith('meta_')})
        meta={'match':'商品编码精确匹配','blocking':[],'notes':[],'category':'快销',
              'product_tags':'','product_tags_known':True,'export_row':i+2,'export_status':'正常'}
        meta.update({k[5:]:v for k,v in fields.items() if k.startswith('meta_')})
        normalized.append((d,meta))
    cfg=engine.config();cfg['parameters']['minimum_reference_units']=minimum
    return {'normalized':normalized,'config':cfg,'stores':{'T':'合成必采测试店'},'hashes':{}}


def calculate(data=None,members=('1',),budget=None):
    p=priority_list.make_profile([{'sku':s,'name':'合成'+s} for s in members],'合成必采清单') if members else None
    return engine.run_calculation(data or loaded(),'T',{'河北仓':(8,5)},False,budget,p)


class MandatoryHoldingTests(unittest.TestCase):
    def test_zero_sales_zero_stock_without_new_product_selection(self):
        for n in (None,'',0,4,30):
            with self.subTest(stockout=n):
                r=calculate(loaded({'N':n,'O':6}));d=r['rows'][0]
                self.assertEqual(sem.order_quantity(d),6)
                self.assertEqual(sem.order_amount(d),12)
                self.assertNotIn('_new_requested',d)
                self.assertEqual(d['_mandatory_holding']['required_quantity'],6)
                self.assertEqual(d['AS'],r['baseline'][0]['AS'])
                self.assertEqual(d['BV'],r['baseline'][0]['BV'])
    def test_existing_available_or_inbound_coverage(self):
        for fields in ({'K':1},{'L':1},{'K':.6,'L':.4},{'K':3,'L':2}):
            with self.subTest(fields=fields):
                d=calculate(loaded(fields))['rows'][0]
                self.assertEqual(sem.order_quantity(d),0)
                self.assertEqual(d['_mandatory_holding']['required_quantity'],0)
    def test_partial_coverage_and_carton_rounding(self):
        for fields,minimum,expected in [({'O':6},1,6),({'K':2,'L':1,'O':6},5,6),
                                         ({'K':.5,'O':.3},1,.6),({'K':-2,'L':-1,'O':2},1,2)]:
            with self.subTest(fields=fields):
                d=calculate(loaded(fields,minimum=minimum))['rows'][0]
                self.assertEqual(sem.order_quantity(d),expected)
                self.assertGreaterEqual(Decimal(str(max(d['K'],0)))+Decimal(str(max(d['L'],0)))+Decimal(str(expected)),Decimal(str(minimum)))
    def test_normal_suggestion_is_not_reduced(self):
        r=calculate(loaded({'H':2,'I':14,'J':60}));d=r['rows'][0]
        self.assertGreater(d['AS'],1);self.assertEqual(sem.order_quantity(d),d['AS'])
        self.assertEqual(d['_mandatory_holding']['added_quantity'],0)
        self.assertEqual(sem.explanation(d),d['BM'])
    def test_ordinary_sku_and_absent_mandatory_not_manufactured(self):
        data=loaded({'E':'normal'})
        before=calculate(data,members=());after=calculate(data,members=('absent',))
        self.assertEqual(before['rows'],after['rows'])
        self.assertEqual(sem.order_quantity(after['rows'][0]),0)
        self.assertEqual(after['must_list']['missing'],1)
        self.assertEqual(len(after['rows']),1)
    def test_floor_precedes_other_mandatory_growth(self):
        r=calculate(loaded({'H':5,'I':35,'J':150,'V':1},{'V':2}),members=('1','2'),budget=3)
        self.assertGreater(r['rows'][0]['AS'],1)
        self.assertEqual([sem.order_quantity(d) for d in r['rows']],[1,1])
        self.assertEqual(r['budget']['spent'],3)
        self.assertEqual(r['summary']['holding_gaps'],0)
    def test_budget_zero_and_shortfall_not_hidden(self):
        r=calculate(loaded({},{}),members=('1','2'),budget=0)
        self.assertEqual(r['budget']['spent'],0)
        self.assertEqual([sem.order_quantity(d) for d in r['rows']],[0,0])
        self.assertEqual(r['summary']['holding_gaps'],2)
        self.assertIn('最低持有仍有缺口',sem.explanation(r['rows'][1]))
        r=calculate(loaded({'O':6,'V':2}),budget=10)
        self.assertEqual(sem.order_quantity(r['rows'][0]),0)
        self.assertEqual(r['summary']['holding_gaps'],1)
    def test_cancel_still_propagates(self):
        event=threading.Event();event.set()
        with self.assertRaises(Cancelled):engine.run_calculation(loaded(),'T',{'河北仓':(8,5)},False,None,None,cancel=event)
    def test_missing_price_stock_or_step_cannot_be_guessed(self):
        for field in ('V','K','O'):
            data=loaded({field:'未知','meta_blocking':['合成输入待核']},{'E':'control'})
            d=calculate(data)['rows'][0]
            self.assertEqual(d['_mandatory_holding']['state'],'待核')
            self.assertFalse(sem.orderable(d))
        d=calculate(loaded({'meta_product_tags_known':False}))['rows'][0]
        self.assertEqual(d['_mandatory_holding']['state'],'待核')
        self.assertFalse(sem.orderable(d))
        self.assertEqual(sem.order_status(d),'待核')
        r=calculate(loaded({'V':0}))
        self.assertEqual(r['summary']['issues'],1)
        self.assertIn('基本售价待核',sem.issue_text(r['rows'][0]))
        self.assertEqual(sem.order_status(r['rows'][0]),'待核')
    def test_known_local_item_does_not_become_warehouse_order(self):
        d=calculate(loaded({'_source':'地采商品'}))['rows'][0]
        self.assertFalse(sem.orderable(d));self.assertEqual(d['_mandatory_holding']['state'],'待核')
    def test_reapply_and_clear_membership_are_deterministic(self):
        r=calculate();snapshot=copy.deepcopy(r)
        profile=priority_list.make_profile([{'sku':'1','name':'合成1'}],'合成必采清单')
        engine.apply_budget(r,None,profile);self.assertEqual(r,snapshot)
        engine.apply_budget(r,None,None)
        self.assertEqual(sem.order_quantity(r['rows'][0]),0)
        self.assertNotIn('_mandatory_holding',r['rows'][0])
    def test_manual_exception_has_explicit_risk_and_restores_exactly(self):
        r=calculate();p=preissue.preview(r,{'1':{'quantity':0,'reason':'合成例外','kind':'manual'}},'2026-11-11')
        self.assertTrue(any(x['kind']=='mandatory_holding' for x in p['risks']))
        after=preissue.finalize(p,[x['id'] for x in p['risks']],current=r)
        self.assertEqual(sem.order_quantity(after['rows'][0]),0)
        self.assertTrue(after['rows'][0]['_holding_gap'])
        self.assertEqual(preissue.original(after),r)
    def test_new_product_reallocation_keeps_mandatory_floor(self):
        for budget in (None,20):
            r=calculate(loaded({'O':2},minimum=5),budget=budget)
            result=new_products.apply(r,{'1':2});d=result['rows'][0]
            self.assertEqual(d['_new_requested'],2)
            self.assertEqual(sem.order_quantity(d),6)
            self.assertEqual(sem.order_amount(d),12)
    def test_new_and_extreme_switches_do_not_cancel_default_must_sell_floor(self):
        data=loaded();data['config']['restock_switches']={'extreme_restore':False,'new_stocking':False}
        d=calculate(data)['rows'][0]
        self.assertEqual(d['AS'],0);self.assertEqual(sem.order_quantity(d),1)
    def test_historical_rows_are_not_reinterpreted(self):
        d=calculate()['rows'][0]
        for k in list(d):
            if k.startswith('_mandatory_'):d.pop(k)
        d['_meta'].pop('execution_exclusions',None)
        d['_meta']['product_tags']='日配';d['_meta']['export_status']='禁采'
        self.assertEqual(sem.order_quantity(d),0) # saved old AS, no retroactive floor
        d.update(AS=3,BV=6)
        self.assertEqual(sem.order_quantity(d),3) # saved old exclusions also stay frozen


class DailyExclusionTests(unittest.TestCase):
    def test_daily_tag_exact_membership_and_raw_preservation(self):
        for raw in ('日配',' 必卖；日配，活动品 ','必卖\n日配'):
            d=calculate(loaded({'meta_product_tags':raw,'H':2,'I':14,'J':60}))['rows'][0]
            self.assertGreater(d['AS'],0);self.assertEqual(sem.order_quantity(d),0)
            self.assertEqual(d['_candidate_qty'],0);self.assertNotIn('_mandatory_holding',d)
            self.assertEqual(d['_meta']['product_tags'],raw)
            self.assertIn('日配',sem.exclusion_reason(d))
        for raw in ('非日配','日配专区',''):
            self.assertEqual(sem.order_quantity(calculate(loaded({'meta_product_tags':raw}))['rows'][0]),1)
    def test_mandatory_never_overrides_disable_stop_forbid_or_refusal(self):
        context={'version':admission.VERSION,'record':{'refusals':{'1':{'reason':'拒收','active':True}}},'store':'T','day':'2026-11-11','revision':'synthetic'}
        for fields in ({'S':'禁用'},{'meta_export_status':'停采'},{'meta_export_status':'禁采'},
                       {'meta_product_tags':'二类医疗'},{'meta_product_tags_known':False},{'meta_product_tags':'日配'}):
            data=loaded(fields)
            data=admission.apply(data,'T',{**context,'record':{}})
            d=calculate(data,budget=100)['rows'][0]
            self.assertEqual(sem.order_quantity(d),0)
            self.assertEqual(d['_candidate_qty'],0)
            self.assertFalse(d.get('_holding_gap'))
        d=calculate(admission.apply(loaded(),'T',context))['rows'][0]
        self.assertEqual(sem.order_quantity(d),0)
    def test_daily_catalog_uses_attributed_warehouse(self):
        catalogs={'河北仓':{'revision':'hb','items':{'1':{'商品标签':'日配'}}},'山东仓':{'revision':'sd','items':{'1':{'商品标签':''}}}}
        self.assertTrue(admission.default_exclusions(admission.catalog_meta(catalogs,'河北仓','1')))
        self.assertEqual(admission.default_exclusions(admission.catalog_meta(catalogs,'山东仓','1')),[])
    def test_new_and_manual_paths_cannot_bypass_daily(self):
        r=calculate(loaded({'meta_product_tags':'日配'}))
        with self.assertRaisesRegex(ValueError,'日配'):new_products.apply(r,{'1':1})
        with self.assertRaisesRegex(ValueError,'日配'):preissue.preview(r,{'1':{'quantity':1,'reason':'尝试绕过','kind':'manual'}},'2026-11-11')


if __name__=='__main__':unittest.main()
