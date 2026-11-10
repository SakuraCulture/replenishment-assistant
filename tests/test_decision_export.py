"""Business projections must retain frozen values and omit technical expansion."""
import copy,unittest
from unittest.mock import patch
import test_archive_orders as fixtures
import decision_export as export
import result_semantics as sem


class DecisionExportTests(unittest.TestCase):
    def test_business_values_actual_parameters_and_frozen_master(self):
        r=fixtures.model.calculate(fixtures.row(E='0001',K=3,L=2));r.update(AU='河北仓',_source='资料匹配',_meta=copy.deepcopy(fixtures.META),_must_sell=True)
        r['_meta'].update(product_tags='日配',product_tags_known=True,warehouse_available=47,
            routing={'primary':'河北仓','secondary':'山东仓','primary_stock':47,'secondary_stock':8},export_status='正常')
        r.update(_effective_probability=.95,_mandatory_holding={'minimum':1,'required_quantity':0,'added_quantity':0},
            _preissue={'kind':'manual','delta':12,'reason':'补齐陈列','automatic_quantity':12,'quantity':24},_technical_junk={'huge_array':[{'ignore':n} for n in range(10000)]})
        cfg=fixtures.engine.config();cfg['parameters']['cap_days']=17
        order={'day':'2026-11-10','store':'T','number':'BH-T-ZC','revision':2,'payload':{'model_version':'frozen-model',
            'result':{'store':'T','store_name':'历史门店','rows':[r],'config':cfg,'budget':{'enabled':False}}}}
        frozen=copy.deepcopy(order)
        with patch.object(export,'master_snapshot',return_value=({'0001':{'虚拟分类':'淘便考核','供应商编码':'00007','供应商名称':'原供应商','采购状态':'启用','可用数':999}},'')):
            headers,rows,count=export.projected(order,'河北仓',repo=object());record=dict(zip(headers,next(rows)))
        self.assertEqual(count,1);self.assertLess(len(headers),125);self.assertEqual(record['门店名称'],'历史门店')
        for key,value in [('商品编码','0001'),('仓商品标签','日配'),('虚拟分类','淘便考核'),('供货仓可用库存',47),('从仓可用库存',8),
                          ('参数·普通库存额度天数',17),('参数·库存目标分位概率',.95),('人工改量原因','补齐陈列'),('模型版本','frozen-model'),('最终签发执行数',sem.order_quantity(r))]:
            self.assertEqual(record[key],value,key)
        self.assertFalse(any(isinstance(v,(dict,list)) for v in record.values()));self.assertEqual(order,frozen)
        self.assertFalse(any('technical' in k or 'ignore' in k for k in headers))

    def test_missing_frozen_fields_remain_blank(self):
        r=fixtures.model.calculate(fixtures.row(E='1'));r.update(AU='河北仓',_source='资料匹配',_meta=copy.deepcopy(fixtures.META))
        order={'day':'2026-11-10','number':'BH-T-ZC','store':'T','revision':1,'payload':{'result':{'rows':[r],'budget':{}}}}
        headers,rows,_=export.projected(order,'河北仓');record=dict(zip(headers,next(rows)))
        self.assertIsNone(record['虚拟分类']);self.assertIsNone(record['采购状态']);self.assertIsNone(record['参数·普通库存额度天数'])
        self.assertIn('历史',record['资料说明'])


if __name__=='__main__':unittest.main()
