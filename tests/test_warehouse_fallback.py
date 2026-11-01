"""Product existence differs from warehouse availability; no user files needed."""
import copy,sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import warehouse_catalog as wc,batch_service as bs,engine
from table_records import bridge_table


def fixture(primary='河北仓',secondary='山东仓'):
    cases=['main_only_empty','main_only_disabled','main_only_stopped','both_empty',
           'child_disabled','main_available','main_unknown','child_only','local']
    sales=[{'仓库/门店编码':'QA','仓库/门店名称':'归属测试','商品编码':s,'商品名称':s,
            '日销':2,'周销':14,'月销':60,'可用库存':0,'在途库存':0,
            '缺货天数（天）':0,'补货状态':'停采' if s=='main_only_stopped' else '正常'} for s in cases]
    catalogs={}
    for wh in (primary,secondary):
        items={}
        for i,s in enumerate(cases):
            if s=='local' or (wh==primary and s=='child_only') or (wh==secondary and s.startswith('main_')):continue
            items[s]=dict.fromkeys(wc.FIELDS)
            items[s].update({'商品编码':s,'商品名称':s,'分类':'饮品' if wh==primary else '家居日用',
              '基本售价':2 if wh==primary else 3,'出库箱规':6 if wh==primary else 3,
              '商品状态':'禁用' if (s=='main_only_disabled' or (s=='child_disabled' and wh==secondary)) else '启用',
              '可用数':None if s=='main_unknown' else 99 if s=='main_available' else 0,'_row':i+12})
        catalogs[wh]={'schema':1,'warehouse':wh,'items':items,'revision':wc.revision(items),
                      'source_path':'fixture/'+wh+'.xlsx','sheet':'原始商品资料','source_sha256':'qa','imported_at':'fixed'}
    loaded=wc.prepare_records(bridge_table(list(sales[0]),sales,'原始销量'),catalogs)
    return loaded,{'primary':primary,'secondary':secondary}


class WarehouseFallbackTests(unittest.TestCase):
    def test_missing_secondary_preserves_entire_primary_pair_and_order(self):
        for primary,secondary in [('河北仓','山东仓'),('山东仓','河北仓')]:
            loaded,route=fixture(primary,secondary);before=copy.deepcopy(loaded)
            out=wc.route_store(loaded,'QA',route)['normalized']
            self.assertEqual([d['E'] for d,m in out],[d['E'] for d,m in loaded['normalized']])
            for i,(d,m) in enumerate(out):
                if not d['E'].startswith('main_only'):continue
                self.assertEqual(d,loaded['warehouse_rows'][primary][i][0])
                self.assertEqual({k:v for k,v in m.items() if k!='routing'},loaded['warehouse_rows'][primary][i][1])
                self.assertEqual(d['AU'],primary);self.assertIn('从仓无该商品',m['routing']['reason'])
                self.assertIn('保留主仓',m['routing']['reason']);self.assertEqual(m['routing']['version'],'0.5.1')
            self.assertEqual(loaded,before)

    def test_existing_secondary_zero_and_disabled_still_secondary(self):
        loaded,route=fixture();rows={d['E']:(d,m) for d,m in wc.route_store(loaded,'QA',route)['normalized']}
        for sku in ('both_empty','child_disabled','child_only'):
            d,m=rows[sku];self.assertEqual((d['AU'],d['V'],d['O']),('山东仓',3,3));self.assertEqual(m['routing']['version'],'0.5.0')
        self.assertEqual(rows['local'][0]['_source'],'地采商品')
        self.assertEqual(rows['local'][0]['AU'],'地采')
        for sku in ('main_available','main_unknown'):self.assertEqual(rows[sku][0]['AU'],'河北仓')
        self.assertIn('待核',rows['main_unknown'][1]['routing']['reason'])

    def test_primary_fields_cycles_calculation_and_exclusion(self):
        loaded,route=fixture();settings={'supply_route':route,'warehouses':{'河北仓':[9,4],'山东仓':[13,7]},'exception':True,'budget':None}
        out=bs.calculate_store(loaded,'QA',settings,None)
        single=bs.calculate_store(loaded,'QA',{**settings,'supply_route':{'primary':'河北仓','secondary':None}},None)
        orig={d['E']:d for d in single['rows']};rows={d['E']:d for d in out['rows']}
        for sku in ('main_only_empty','main_only_disabled','main_only_stopped'):
            a=copy.deepcopy(rows[sku]);b=copy.deepcopy(orig[sku]);a['_meta'].pop('routing');b['_meta'].pop('routing')
            self.assertEqual(a,b);self.assertEqual((a['AG'],a['AH'],a['V'],a['BD']),(9,4,2,6))
            self.assertEqual(a['_meta']['product_row'],next(m['product_row'] for d,m in loaded['warehouse_rows']['河北仓'] if d['E']==sku))
        self.assertGreater(engine.order_quantity(rows['main_only_empty']),0)
        for sku in ('main_only_disabled','main_only_stopped','child_disabled'):self.assertEqual(engine.order_quantity(rows[sku]),0)
        for d in out['rows']:
            if engine.orderable(d):self.assertEqual(engine.order_quantity(d)%d['BD'],0)

    def test_invalid_code_is_not_manufactured_into_a_match(self):
        loaded,route=fixture()
        for pairs in loaded['warehouse_rows'].values():
            d,m=pairs[0];d['_source']='匹配待核';m['match']='编码待核';m['blocking']=['商品编码待核']
        d,m=wc.route_store(loaded,'QA',route)['normalized'][0]
        self.assertEqual(d['_source'],'匹配待核');self.assertIn('商品编码待核',m['blocking'])


if __name__=='__main__':unittest.main()
