"""Single-warehouse ownership follows the selected route, not installed catalogs."""
import copy
import unittest
from test_warehouse_fallback import fixture
import batch_service as bs
import engine
import preissue
import warehouse_catalog as wc


class SingleWarehouseScopeTests(unittest.TestCase):
    def directions(self):
        return [('山东仓','河北仓'),('河北仓','山东仓')]

    def test_other_warehouse_only_is_local_in_both_directions(self):
        for primary,other in self.directions():
            with self.subTest(primary=primary):
                loaded,_=fixture(primary,other);before=copy.deepcopy(loaded)
                route={'primary':primary,'secondary':None}
                out=wc.route_store(loaded,'QA',route)
                rows={d['E']:(d,m) for d,m in out['normalized']}
                d,m=rows['child_only']
                self.assertEqual((d['_source'],d['AU']),('地采商品','地采'))
                self.assertIsNone(d['V']);self.assertNotEqual(d['O'],3)
                self.assertEqual(m['match'],'资料未匹配')
                self.assertEqual(m['blocking'],['待补商品资料'])
                self.assertIn(primary,m['routing']['reason'])
                self.assertIn('本次地采',m['routing']['reason'])
                self.assertEqual(m['routing']['version'],'0.5.2')
                self.assertEqual(loaded,before)

    def test_unused_catalog_presence_has_no_effect_on_routed_rows(self):
        for primary,other in self.directions():
            loaded,_=fixture(primary,other)
            only=copy.deepcopy(loaded)
            only['warehouse_rows']={primary:only['warehouse_rows'][primary]}
            only['catalogs']={primary:only['catalogs'][primary]}
            route={'primary':primary,'secondary':None}
            self.assertEqual(wc.route_store(loaded,'QA',route)['normalized'],
                             wc.route_store(only,'QA',route)['normalized'])

    def test_selected_catalog_fields_and_existing_exclusions_survive(self):
        for primary,other in self.directions():
            loaded,_=fixture(primary,other)
            settings={'supply_route':{'primary':primary,'secondary':None},
                      'warehouses':{'河北仓':[9,4],'山东仓':[13,7]},'exception':True,'budget':None}
            result=bs.calculate_store(loaded,'QA',settings,None)
            rows={d['E']:d for d in result['rows']}
            for sku in ('main_only_empty','main_only_disabled','main_only_stopped','main_unknown','main_available'):
                self.assertEqual(rows[sku]['AU'],primary)
                self.assertEqual((rows[sku]['V'],rows[sku]['BD']),(2,6))
                self.assertEqual([rows[sku]['AG'],rows[sku]['AH']],settings['warehouses'][primary])
            self.assertGreater(engine.order_quantity(rows['main_only_empty']),0)
            for sku in ('main_only_disabled','main_only_stopped'):
                self.assertEqual(engine.order_quantity(rows[sku]),0)
            self.assertIn('可用数待核',rows['main_unknown']['_meta']['routing']['reason'])
            for sku in ('child_only','local'):
                self.assertFalse(engine.issue_text(rows[sku]))
                self.assertFalse(engine.orderable(rows[sku]))
                self.assertIn('地采',preissue.blocked(rows[sku]))
            self.assertEqual(result['summary']['local'],2)

    def test_missing_selected_catalog_is_still_blocked(self):
        loaded,_=fixture();loaded['catalogs'].pop('河北仓')
        with self.assertRaisesRegex(ValueError,'河北仓资料尚未导入'):
            wc.route_store(loaded,'QA',{'primary':'河北仓','secondary':None})

    def test_invalid_identity_is_not_reclassified_as_local(self):
        for primary,other in self.directions():
            loaded,_=fixture(primary,other)
            d,m=next(pair for pair in loaded['warehouse_rows'][primary] if pair[0]['E']=='child_only')
            d['_source']='匹配待核';m['match']='编码待核';m['blocking']=['商品编码重复待核']
            out=wc.route_store(loaded,'QA',{'primary':primary,'secondary':None})
            d,m=next(pair for pair in out['normalized'] if pair[0]['E']=='child_only')
            self.assertEqual(d['_source'],'匹配待核')
            self.assertIn('商品编码重复待核',m['blocking'])

    def test_local_classification_does_not_hide_other_invalid_inputs(self):
        loaded,_=fixture();d,m=next(pair for pair in loaded['warehouse_rows']['河北仓'] if pair[0]['E']=='child_only')
        m['blocking'].append('月销不是有效数字')
        d,m=next(pair for pair in wc.route_store(loaded,'QA',{'primary':'河北仓','secondary':None})['normalized'] if pair[0]['E']=='child_only')
        self.assertEqual(d['_source'],'地采商品')
        self.assertEqual(engine.issue_text({**d,'_meta':m}),'月销不是有效数字')

    def test_dual_warehouse_still_routes_to_other_warehouse(self):
        for primary,other in self.directions():
            loaded,route=fixture(primary,other)
            rows={d['E']:(d,m) for d,m in wc.route_store(loaded,'QA',route)['normalized']}
            d,m=rows['child_only']
            self.assertEqual((d['_source'],d['AU'],d['V'],d['O']),('资料匹配',other,3,3))
            self.assertEqual(m['routing']['version'],'0.5.0')


if __name__=='__main__':unittest.main()
