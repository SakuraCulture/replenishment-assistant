"""Portable read-only projection/filter contracts; independent core preview oracle."""
import copy
import unittest
from decimal import Decimal
from test_preissue import row,result,edit,listing,commit,DAY
import preissue
import normal_exports
import review_amounts
import review_filters as f
from draft_review import DraftReview


class DraftReviewTests(unittest.TestCase):
    def verify(self,r,edits,lists=None):
        before=copy.deepcopy(r);s=r['store'];values=DraftReview({s:r},{s:edits},{s:lists or {}})
        preview=preissue.preview(r,edits,DAY,lists=lists or {})['result'];expected=review_amounts.summarize(preview['rows']);actual=values.summary(s)
        for key,v in expected.items():self.assertEqual(actual[key],v,key)
        self.assertEqual(actual['total'],Decimal(str(preview['summary']['amount'])))
        self.assertEqual(r,before);return values,preview
    def test_manual_new_overlap_zero_and_restoration(self):
        r=result([row(),dict(row('new',0,1,2),H=0,I=0,J=0,K=0,L=0,N=0)])
        values,_=self.verify(r,{'a':edit(0),'new':edit(5)})
        self.assertEqual(values.summary()['manual_amount'],Decimal(10));self.assertEqual(values.summary()['manual_delta'],Decimal(-14));self.assertEqual(values.summary()['new_amount'],Decimal(10))
        published=commit(r,{'new':edit(5)})
        restored,_=self.verify(published,{})
        self.assertEqual(restored.summary()['manual_count'],0);self.assertEqual(restored.summary()['new_amount'],0)
    def test_same_positive_quantity_has_no_phantom_manual_money(self):
        self.verify(result(),{'a':edit(24)})
    def test_prices_more_than_two_decimals_sum_before_rounding(self):
        values,_=self.verify(result([row('a',1,1,.014),row('b',1,1,.014)]),{})
        self.assertEqual(values.summary()['total'],Decimal('.03'))
    def test_list_can_reclassify_no_sales_with_inbound(self):
        r=result([dict(row('a',24,24,2),H=0,I=0,J=0,K=0,L=6,N=5)])
        l=listing(['a']);e={'a':dict(edit(24),kind='new',list_revision=l['revision'])}
        values,p=self.verify(r,e,{l['revision']:l})
        self.assertEqual(f.group(values.views[('S1','a')]),'本月新品');self.assertEqual(values.summary()['new_amount'],48)
        self.assertEqual(normal_exports.groups(p,None)['本月新品'][0]['E'],'a')
    def test_multiple_stores_and_incremental_update(self):
        r=result();other=dict(result([row('a',12,12,3)]),store='S2');edits={'S1':{},'S2':{}};v=DraftReview({'S1':r,'S2':other},edits,{'S1':{},'S2':{}})
        edits['S1']['a']=edit(48);v.update(('S1','a'))
        self.assertEqual(v.summary('S1')['manual_delta'],24);self.assertEqual(v.summary('S2')['manual_count'],0)
        self.assertEqual(v.summary()['total'],v.summary('S1')['total']+v.summary('S2')['total'])
    def test_unknown_and_excluded_not_zero_cost(self):
        r=result([dict(row(),V=None,BV='待核'),dict(row('stop'),S='禁用')]);v=DraftReview({'S1':r},{'S1':{}},{'S1':{}})
        self.assertEqual(v.summary()['total_unknown'],1);self.assertEqual(v.summary()['manual_count'],0)
    def test_budget_and_reason_changes_match_preview(self):
        r=result([row('a',24,24,2),row('b',12,12,2)],48)
        self.verify(r,{'a':edit(0),'b':edit(12)})


class FilterTests(unittest.TestCase):
    def test_group_matches_export_without_reordering(self):
        rows=[row(),dict(row('new',0,1,2),H=0,I=0,J=0,K=0,L=0,N=0),dict(row('month',0,1,2),H=0,I=0,J=0,K=0,L=0,N=0,_preissue={'current_month':True}),row('none',0),dict(row('disabled'),S='禁用')]
        r=result(rows);groups=normal_exports.groups(r,None)
        for key,label in [('中心仓动销补货清单','动销补货'),('本月新品','本月新品'),('往期新品','往期新品')]:self.assertEqual([d['E'] for d in r['rows'] if f.group(d)==label],[d['E'] for d in groups[key]])
    def test_independent_dimensions_and_search(self):
        d=dict(row(),F='001234',D='测试 饼干',_preissue={'kind':'manual'},_budget_active=True,_allocated_qty=12,_allocated_amount=12,_budget_gap=12)
        self.assertTrue(f.accepts(d,{'quantity':'正数量','adjustment':'人工改量','budget':'部分安排'}))
        self.assertFalse(f.accepts(d,{'quantity':'零数量','adjustment':'人工改量'}))
        self.assertTrue(f.matches(f.search_text(d),'001234 饼干'));self.assertFalse(f.matches(f.search_text(d),'001234 饮料'))
    def test_unknown_focus_is_not_claimed_previous_month(self):
        d=dict(row(),J=0,K=0,L=0,N=0)
        self.assertEqual(f.group(d,(),False),'新品归期待核')
    def test_option_counts_hold_other_dimensions(self):
        rows=[row('a'),dict(row('b',0),_preissue={'kind':'manual'}),dict(row('c'),_preissue={'kind':'manual'})]
        counts=f.facet_counts(rows,{'quantity':'正数量','adjustment':'人工改量'})
        self.assertEqual(counts['quantity']['all'],2);self.assertEqual(counts['quantity']['零数量'],1);self.assertEqual(counts['adjustment']['all'],2)
        self.assertEqual(counts['adjustment']['人工改量'],1)
    def test_incremental_counts_equal_fresh_count(self):
        values={'adjustment':'人工改量'};rows={'a':row('a'),'b':dict(row('b'),_preissue={'kind':'manual'})};counter=f.FacetCounter(values,set(),True)
        for k,d in rows.items():counter.update(k,d,True)
        rows['a']=dict(rows['a'],_execution_qty=0,_preissue={'kind':'manual'});counter.update('a',rows['a'],True)
        expected=f.facet_counts(rows.values(),values)
        for field in counter.counts:
            for key in set(counter.counts[field])|set(expected[field]):self.assertEqual(counter.counts[field].get(key,0),expected[field].get(key,0))
        counter.update('b',rows['b'],False);self.assertEqual(counter.counts['quantity']['all'],1)


if __name__=='__main__':unittest.main()
