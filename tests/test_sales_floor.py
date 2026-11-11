"""Actual input/store entry: sales floors require two real numeric zero stocks."""
import copy,unittest,tempfile
from pathlib import Path
import openpyxl
import engine,model,input_adapter as adapter,normal_exports as ne,priority_list,result_semantics as sem
from table_records import bridge_table

BASE={'H':2.86,'I':24,'J':73,'K':0,'L':40,'N':0,'O':20,'V':.75}

def prepared(fields=None,*,moq=12,cap=21,status='启用',export_status='正常',tags=''):
    f={**BASE,**(fields or {})}
    sale={'仓库/门店编码':'QA_FLOOR','仓库/门店名称':'隔离保底测试店','商品编码':'QA001','商品名称':'汾煌雪梅20g/袋（参数模拟）',
          '日销':f['H'],'周销':f['I'],'月销':f['J'],'可用库存':f['K'],'在途库存':f['L'],'缺货天数（天）':f['N'],'补货状态':export_status,'单位类型':'袋'}
    product={'商品编码':'QA001','商品名称':'隔离商品','分类':'休闲食品','基本售价':f['V'],'出库箱规':f['O'],'商品状态':status,'商品标签':tags,'仓归属':'山东仓'}
    control={**sale,'商品编码':'CONTROL','日销':0,'周销':0,'月销':0,'可用库存':0,'在途库存':0,'缺货天数（天）':0,'补货状态':'正常'}
    master={**product,'商品编码':'CONTROL','商品状态':'启用','商品标签':'','出库箱规':1,'基本售价':1}
    loaded=engine.load_records(bridge_table(list(sale),[sale,control],'商品补货建议'),bridge_table(list(product),[product,master],'Sheet1'),sales_ref='isolated:sales',product_ref='isolated:products')
    loaded['config']['parameters'].update(moq_days=moq,cap_days=cap)
    for d,m in loaded['normalized']:d.update(AI=moq,AJ=6)
    return loaded

def calculate(fields=None,*,exception=False,must=False,budget=None,**kw):
    data=prepared(fields,**kw);before=copy.deepcopy(data)
    profile=priority_list.make_profile([{'sku':'QA001' if must else 'NOT_PRESENT','name':'隔离必采'}],'隔离必采清单') if must or budget is not None else None
    result=engine.run_calculation(data,'QA_FLOOR',{'山东仓':(8,5)},exception,budget,profile)
    if data!=before:raise AssertionError('Store calculation changed original prepared inputs')
    return result,result['rows'][0]

class SalesFloor(unittest.TestCase):
    def test_A_double_zero_keeps_floor_and_does_not_stack_it_on_normal_demand(self):
        _,d=calculate({'L':0});self.assertTrue(model.sales_floor_eligible(d));self.assertEqual((d['AQ'],d['AS']),(52,60));self.assertEqual(ne.decision(d),'动销补货')
        _,d=calculate({'H':.07,'I':1,'J':1,'L':0});self.assertEqual((d['AR'],d['BP'],d['BQ'],d['AS']),(0,20,20,20))
        self.assertEqual(ne.decision(d),'断货保底');self.assertIn('可用与在途均为0',d['BM'])
    def test_B_inbound_and_failed_ordinary_qualification_cannot_add_a_floor(self):
        for exception in (False,True):
            with self.subTest(exception=exception):
                _,d=calculate(moq=0,exception=exception)
                self.assertEqual((d['P'],d['AQ'],d['BI'],d['AR'],d['BP'],d['BQ'],d['AS']),(8,12,0,20,0,0,0))
                self.assertEqual(ne.decision(d),'普通补货资格未通过');self.assertFalse(d['_audit']['applied'])
                self.assertIn('不触发销售型断货保底',sem.explanation(d))
    def test_C_inbound_does_not_block_qualified_net_demand(self):
        _,d=calculate();self.assertEqual((d['BI'],d['AQ'],d['AR'],d['AS']),(1,12,20,20))
        self.assertAlmostEqual(d['AP']+d['AL'],52);self.assertEqual(ne.decision(d),'动销补货');self.assertFalse(model.sales_floor_eligible(d))
    def test_D_stock_or_inbound_cover_target_even_when_ordinary_qualification_passes(self):
        for fields in ({'L':80},{'K':80,'L':0}):
            _,d=calculate(fields,moq=100);self.assertEqual((d['BI'],d['AQ'],d['BQ'],d['AS']),(1,0,0,0));self.assertEqual(ne.decision(d),'目标已覆盖')
    def test_E_positive_negative_and_tiny_stock_do_not_masquerade_as_double_zero(self):
        for fields in ({'K':1,'L':0},{'K':-40,'L':40},{'K':-5,'L':0},{'K':0,'L':-1},{'K':0,'L':1e-10}):
            _,d=calculate(fields);self.assertFalse(model.sales_floor_eligible(d));self.assertEqual(d['BQ'],0);self.assertGreater(d['AS'],0)
            self.assertNotEqual(ne.decision(d),'断货保底')
        _,d=calculate({'K':-40,'L':40});self.assertEqual(d['K']+d['L'],0);self.assertEqual(d['AS'],60)
    def test_missing_invalid_and_nonfinite_stock_stays_pending(self):
        for fields in ({'K':None},{'L':None},{'L':'未知'},{'K':float('inf')},{'L':True}):
            _,d=calculate(fields);self.assertFalse(model.sales_floor_eligible(d));self.assertEqual(d['AS'],'输入待核');self.assertEqual(sem.order_quantity(d),'待核');self.assertEqual(ne.decision(d),'资料待核')
    def test_small_inbound_cannot_use_BQ_to_bypass_funds(self):
        _,d=calculate({'H':.07,'I':1,'J':1,'L':.5},cap=0)
        self.assertEqual((d['BI'],d['AR'],d['BQ'],d['BU'],d['AS']),(1,20,0,0,0));self.assertEqual(ne.decision(d),'资金约束未安排')
    def test_postcheck_rejects_reintroduced_BP_or_BQ_floor_and_unexplained_quantity(self):
        _,d=calculate(moq=0);tampered=copy.deepcopy(d);tampered.update(BP=20,BR=20,BU=20,AS=20)
        self.assertTrue(any('非双零' in r[1] for r in adapter.business_checks([tampered],model.Rules())['failures']))
        _,d=calculate({'H':.07,'I':1,'J':1,'L':.5},cap=0);tampered=copy.deepcopy(d);tampered.update(BQ=20,AS=20)
        self.assertTrue(any('非双零' in r[1] for r in adapter.business_checks([tampered],model.Rules(cap_days=0))['failures']))
    def test_independent_one_box_exception_is_kept_and_labelled_separately(self):
        _,d=calculate({'H':.07,'I':1,'J':1,'L':.5},cap=0,exception=True)
        self.assertEqual(d['AS'],20);self.assertTrue(d['_audit']['applied']);self.assertEqual(ne.decision(d),'一箱恢复');self.assertEqual(d['BQ'],0)
    def test_mandatory_holding_remains_separate_and_counts_inbound(self):
        _,d=calculate({'H':.07,'I':1,'J':1,'L':.5},must=True)
        self.assertEqual((d['AS'],sem.order_quantity(d)),(0,20));self.assertEqual(ne.decision(d),'必采最低持有')
        _,d=calculate(moq=0,must=True);self.assertEqual(sem.order_quantity(d),0);self.assertEqual(d['_mandatory_holding']['required_quantity'],0)
    def test_budget_and_admission_still_control_execution(self):
        _,d=calculate(budget=0);self.assertEqual((d['AS'],sem.order_quantity(d)),(20,0));self.assertEqual(ne.decision(d),'预算暂缓')
        for kw in ({'status':'禁用'},{'tags':'日配'},{'export_status':'停采'},{'export_status':'禁采'}):
            _,d=calculate(**kw);self.assertEqual(sem.order_quantity(d),0)
    def test_zero_sales_and_other_restoration_rules_keep_original_scope(self):
        _,d=calculate({'H':0,'I':0,'J':0,'L':0});self.assertEqual(d['AS'],0);self.assertEqual(ne.decision(d),'新品未选')
        for n,reason in ((4,'零销恢复'),(30,'极端缺货恢复')):
            for inbound in (0,.5):
                _,d=calculate({'H':0,'I':0,'J':0,'L':inbound,'N':n},cap=0)
                self.assertEqual(d['AS'],20);self.assertEqual(ne.decision(d),reason)
    def test_history_without_new_rule_marker_keeps_original_short_reason_and_quantity(self):
        _,d=calculate({'H':.07,'I':1,'J':1,'L':0});d.pop('_floor_rule');before=copy.deepcopy(d)
        self.assertEqual(ne.decision(d),'最低持有');self.assertEqual(d,before)
    def test_inbound_cap_warning_survives_without_manufacturing_a_floor(self):
        _,d=calculate({'H':.07,'I':1,'J':1,'L':.5});self.assertEqual((d['AR'],d['AS']),(0,0));self.assertNotIn('_inbound_cap',d)
        before=copy.deepcopy(d);risk=sem.inbound_risk(d);self.assertIsNotNone(risk);self.assertEqual(d,before);self.assertIn('不自动追加',risk['text'])
        legacy=copy.deepcopy(d);legacy.pop('_floor_rule');self.assertIsNone(sem.inbound_risk(legacy))
    def test_actual_sign_archive_and_excel_keep_distinct_floor_sources(self):
        import work_repository as wr,normal_orders as no,new_product_focus as focus
        from version_info import MODEL_VERSION
        cases=[({'H':.07,'I':1,'J':1,'L':0},{},'断货保底'),({}, {},'动销补货'),
               ({'H':.07,'I':1,'J':1,'L':.5},{'cap':0,'exception':True},'一箱恢复'),
               ({'H':.07,'I':1,'J':1,'L':.5},{'must':True},'必采最低持有')]
        for fields,options,reason in cases:
            with self.subTest(reason=reason),tempfile.TemporaryDirectory() as folder:
                root=Path(folder);work=wr.Repository(root/'settings');repo=no.Repository(work)
                result,d=calculate(fields,**options);day=wr.today()
                payload={'schema':1,'day':day,'model_version':MODEL_VERSION,'input_dirty':False,'results':{'QA_FLOOR':result},
                         'states':{'QA_FLOOR':{'state':'已完成','name':'隔离保底测试店'}},'new_focus':focus.empty(day[:7])}
                saved=work.save_job(day,payload);order=repo.confirm(payload,['QA_FLOOR'],saved['revision'],{'QA_FLOOR':None})[0]
                before=copy.deepcopy(order['payload']);target=root/'signed.xlsx';ne.export(order,target,warehouse='山东仓')
                book=openpyxl.load_workbook(target)
                try:
                    values=[r for ws in book if ws.sheet_state=='visible' for r in list(ws.values)[1:] if len(r)>20 and r[4]=='QA001']
                    self.assertEqual(len(values),1);self.assertEqual((values[0][16],values[0][20]),(20,reason))
                finally:book.close()
                self.assertEqual(order['payload'],before)
                self.assertEqual(order['payload']['result']['rows'][0]['_floor_rule'],model.FLOOR_RULE)
