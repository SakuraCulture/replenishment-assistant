"""Saved version and recovery explanations agree across read-only views and Excel."""
from contextlib import closing
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from openpyxl import load_workbook

import business_presenter as presenter
import decision_export
import model
import result_semantics as sem
from test_sales_floor import calculate
import xlsx_exports


def recovery_row(**fields):
    row=model.calculate({'H':0,'I':0,'J':0,'K':fields.pop('K',0),'L':0,'N':68,'O':1,
                         'V':12,'AB':.4,'AC':.5,'AD':.25},
                        extreme_restore=fields.pop('enabled',True))
    return {**row,'_source':'资料匹配','_meta':{'blocking':[]},'G':'双',**fields}


class HistoryModelDisplay(unittest.TestCase):
    def test_outer_inner_and_unknown_versions_match_history_and_excel(self):
        base, row=calculate()
        row.update(E='QA001',AU='山东仓',BF=6,AP=2.6704125,AL=3.3295875,AQ=3)
        for outer,inner,version,basis,formula in (
            ('0.3.6-rules',None,'0.3.6-rules','趋势调整后需求 AP','max(Q − AP, 0)'),
            ('0.3.6-rules','0.3.8-rules','0.3.6-rules','趋势调整后需求 AP','max(Q − AP, 0)'),
            (None,'0.3.6-rules','0.3.6-rules','趋势调整后需求 AP','max(Q − AP, 0)'),
            (None,None,'未留存','未留存','未留存'),
            ('0.3.7-rules',None,'0.3.7-rules','趋势调整前需求 AO','max(Q − AO, 0)'),
            (None,'0.3.8-rules','0.3.8-rules','趋势调整前需求 AO','max(Q − AP, 0)'),
        ):
            with self.subTest(outer=outer,inner=inner),TemporaryDirectory() as temp:
                result={**base,'rows':[row]}
                if inner:result['version']={'model_version':inner}
                payload={'result':result}
                if outer:payload['model_version']=outer
                order={'day':'2026-11-20','store':result['store'],'number':'BH-T-ZC',
                       'warehouse':'山东仓','revision':1,'payload':payload}
                frozen=deepcopy(order)
                for scope in ('决策总览','商品完整计算依据'):
                    fields={name:v for name,v,_,_ in presenter.historical(None,order,scope,'QA001')}
                    self.assertEqual((fields['模型版本'],fields['泊松均值依据'],fields['安全库存公式']),(version,basis,formula))
                    self.assertEqual((fields['泊松分位数 Q'],fields['安全库存量'],fields['净需求']),('6','3.3295875','3'))
                headers,rows,_=decision_export.projected(order,'山东仓')
                excel=dict(zip(headers,next(rows)));book=Path(temp)/'store.xlsx';batch=Path(temp)/'batch.xlsx'
                self.assertEqual((excel['模型版本'],excel['泊松均值依据'],excel['安全库存公式']),(version,basis,formula))
                self.assertEqual(excel['最终签发执行数'],sem.order_quantity(row))
                xlsx_exports.export_xlsx(result,book,model_version=outer)
                with closing(load_workbook(book,read_only=True,data_only=True)) as wb:
                    fields=dict(list(wb['运行概览'].values)[1:])
                    self.assertEqual((fields['模型版本'],fields['泊松均值依据'],fields['安全库存公式']),(version,basis,formula))
                code=result['store'];states={code:{'name':result['store_name'],'state':'已完成'}}
                xlsx_exports.export_batch({code:result},states,batch,model_version=outer)
                with closing(load_workbook(batch,read_only=True,data_only=True)) as wb:
                    fields=dict(list(wb['版本及参数'].values)[1:])
                    self.assertEqual((fields[code+' / model_version'],fields[code+' / 泊松均值依据'],
                                      fields[code+' / 安全库存公式']),(version,basis,formula))
                self.assertEqual(order,frozen)

    def test_extreme_recovery_separates_rule_quantity_from_subsequent_execution(self):
        for row,cause,final in (
            (recovery_row(),'按一个出库单位恢复 1 双','最终执行量 1 双'),
            (recovery_row(S='禁用'),'按一个出库单位恢复 1 双','最终执行量 0 双'),
            (recovery_row(K=1),'已有最低覆盖','最终执行量 0 双'),
            (recovery_row(enabled=False),'关闭自动恢复','最终执行量 0 双'),
        ):
            before=deepcopy(row);reason=sem.net_demand_reason(row)
            self.assertIn(cause,reason);self.assertIn(final,reason)
            self.assertEqual(sem.explanation(row),reason);self.assertEqual(row,before)
        self.assertIn('商品资料禁用',sem.net_demand_reason(recovery_row(S='禁用')))

    def test_historical_zero_without_reason_and_ordinary_demand(self):
        old=recovery_row(K=1);old.pop('BM')
        self.assertEqual((old['BO'],old['AQ'],sem.net_demand_reason(old)),(0,0,'未留存'))
        note=sem.net_demand_reason(dict(AP=1.9074375,AL=2.59375,K=3,L=0,AQ=1.5011875))
        self.assertIn('趋势后需求 1.9074375 ＋ 安全库存 2.59375 − 可用库存 3 − 在途库存 0',note)
