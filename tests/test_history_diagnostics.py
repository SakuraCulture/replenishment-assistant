import copy,sys,unittest
from pathlib import Path
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'src'),str(Path(__file__).parent)]
import history_diagnostics as h
import order_analytics as a


def row(day,warehouse='山东仓',**overrides):
    return {'store':'T','store_name':'测试店','sku':'001','name':'品','unit':'包','day':day,'warehouse':warehouse,'group':'必采','scope':'纳入',
        'observations':{'K':0,'L':0,'J':30},'quantities':dict.fromkeys(a.STAGES,10),'valid':dict.fromkeys(a.STAGES,True),
        'decision':{},**overrides}

def report(rows):return {'rows':rows,'snapshot_id':'frozen','as_of':'2026-11-05','query':{}}

class HistoryTests(unittest.TestCase):
    def test_multiple_rounds_downstream_cuts_not_parameter_cause(self):
        prior=row('2026-11-01');prior['quantities'].update(signed=8,**{'return':5,'payment':4,'receipt':3})
        result=h.review(report([row('2026-11-04'),prior]));latest=result['items'][-1]
        self.assertEqual(len(latest['evidence']),4);self.assertIn('不能直接归因参数',latest['hypothesis'])
        self.assertEqual(result['stores'][0]['repeated_stockout_skus'],1)
    def test_completed_is_hypothesis_not_on_time_proof(self):
        result=h.review(report([row('2026-11-01'),row('2026-11-04')]))
        self.assertIn('疑点',result['items'][-1]['hypothesis']);self.assertIn('不证明准时',result['items'][-1]['limitations'][0])
    def test_cross_warehouse_same_day_one_round_no_stock_sum(self):
        result=h.review(report([row('2026-11-01'),row('2026-11-01','河北仓')]))
        self.assertEqual(result['stores'][0]['rounds'],1);self.assertEqual(result['stores'][0]['repeated_stockout_skus'],0)
        self.assertEqual(result['stores'][0]['stockout_observations'],1)
    def test_unknown_unit_or_downstream_prevents_parameter_blame(self):
        arow=row('2026-11-01');arow['valid']['receipt']=False
        latest=h.review(report([arow,row('2026-11-04')]))['items'][-1]
        self.assertIn('补齐',latest['hypothesis'])
        latest=h.review(report([arow,row('2026-11-04',unit='瓶')]))['items'][-1]
        self.assertEqual(latest['hypothesis'],'没有足够证据建议调参')
    def test_excluded_and_pending_not_used_to_claim_better_outcomes(self):
        result=h.review(report([row('2026-11-01',scope='排除'),row('2026-11-02',scope='待核'),row('2026-11-04')]))
        self.assertEqual(len(result['items']),1);self.assertEqual(result['stores'][0]['pending_rows'],1)
    def test_round_limit_real_count_and_immutability(self):
        original=report([row(f'2026-11-{i:02}') for i in range(1,11)]);before=copy.deepcopy(original)
        result=h.review(original,8);self.assertEqual(result['stores'][0]['rounds'],8)
        self.assertEqual(result['stores'][0]['days'][-1],'2026-11-03');self.assertEqual(original,before)
    def test_high_coverage_and_same_day_ambiguity(self):
        first=row('2026-11-01',observations=dict(K=20,L=10,J=30,AN=1,AM=1,AG=7,AH=3))
        second=copy.deepcopy(first);second['day']='2026-11-04'
        r=h.review(report([first,second]));self.assertEqual(r['stores'][0]['repeated_high_coverage_skus'],1);self.assertEqual(r['items'][-1]['coverage'],30)
        duplicate=copy.deepcopy(first);duplicate['warehouse']='河北仓';r=h.review(report([first,duplicate,row('2026-11-04')]))
        self.assertEqual(r['items'][-1]['evidence'],[]);self.assertIn('歧义',' '.join(r['items'][-1]['limitations']))

if __name__=='__main__':unittest.main()
