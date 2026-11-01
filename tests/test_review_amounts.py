"""Derived money display only; synthetic inputs and independent expectations."""
import copy
from decimal import Decimal
from types import SimpleNamespace
import unittest
from test_preissue import row, result, commit, edit, listing, DAY
import review_amounts as money
import preissue
import new_products
import normal_exports


def new_row(sku='new', price=2):
    return dict(row(sku,0,1,price), H=0,I=0,J=0,K=0,L=0,N=None)


class ReviewMoneyTests(unittest.TestCase):
    def test_increase_decrease_and_zero_against_automatic(self):
        r=result([row('up',24,24,2),row('down',48,24,3),row('zero',24,24,4)])
        changed=commit(r,{'up':edit(48),'down':edit(24),'zero':edit(0)})
        before=copy.deepcopy(changed);m=money.summarize(changed['rows'])
        self.assertEqual(m['manual_amount'],Decimal(168))
        self.assertEqual(m['manual_delta'],Decimal(-120))
        self.assertEqual(m['manual_count'],3);self.assertEqual(m['new_amount'],0)
        self.assertEqual(changed,before)
        self.assertEqual([list(d) for d in changed['rows']],[list(d) for d in before['rows']])

    def test_manual_new_overlap_does_not_reallocate(self):
        r=new_products.apply(result([new_row()]),{'new':3})
        r=commit(r,{'new':edit(5)});before=copy.deepcopy(r);m=money.summarize(r['rows'])
        self.assertEqual((m['manual_amount'],m['manual_delta'],m['new_amount']),(10,4,10))
        self.assertEqual(r['summary']['amount'],10);self.assertEqual(r,before)

    def test_budget_uses_final_allocated_not_requested(self):
        r=new_products.apply(result([new_row()],budget=5),{'new':4})
        m=money.summarize(r['rows'])
        self.assertEqual(m['new_amount'],4);self.assertEqual(r['rows'][0]['_candidate_amount'],8)
        self.assertEqual(m['manual_count'],0)
        zero=new_products.apply(result([new_row()],budget=0),{'new':4})
        self.assertEqual(money.summarize(zero['rows'])['new_amount'],0)

    def test_new_candidates_group_union_and_focus(self):
        rows=[new_row('old'),new_row('focused'),dict(new_row('list'),N=40)]
        r=result(rows);li=listing(['list']);filled=preissue.list_edits(r,li,'minimum')
        r=commit(r,filled['edits'],lists=filled['lists'])
        for profile in (None,{'items':[{'sku':'focused','focused':True}]}):
            g=normal_exports.groups(r,profile)
            expected={d['E'] for name in ['本月新品','往期新品'] for d in g[name]}
            self.assertEqual({d['E'] for d in r['rows'] if money.is_new(d)},expected)
        m=money.summarize(r['rows']);self.assertEqual(m['new_count'],3)
        self.assertEqual(m['new_amount'],2);self.assertEqual(m['manual_count'],0)

    def test_plan_promotion_not_direct_manual(self):
        for kind in ['plan','promotion']:
            li=listing(['a']);li['kind']=kind
            import stocking_lists
            li['revision']=stocking_lists.revision(li)
            r=result();filled=preissue.list_edits(r,li,'target',48)
            r=commit(r,filled['edits'],lists=filled['lists'])
            self.assertEqual(money.summarize(r['rows'])['manual_amount'],0)

    def test_excluded_local_and_missing_price(self):
        rows=[dict(new_row('disabled'),S='禁用'),dict(new_row('local'),_source='地采商品'),
              dict(new_row('stopped'),_meta={'blocking':[],'notes':[],'export_status':'停采'}),
              dict(new_row('unknown'),V=None,BV='待核')]
        m=money.summarize(rows)
        self.assertEqual((m['new_count'],m['new_unknown'],m['new_amount']),(1,1,0))
        self.assertTrue(money.amount_text(m,'new').startswith('已计价'))
        self.assertIn('1款金额待核',money.count_text(m,'new'))

    def test_legacy_missing_audit_no_invented_delta(self):
        d=row();self.assertEqual(money.summarize([d])['manual_count'],0)
        d['_preissue']={'kind':'manual','quantity':24}
        m=money.summarize([d]);self.assertEqual(m['manual_amount'],24)
        self.assertEqual(m['delta_unknown'],1);self.assertIn('增减待核',money.delta_text(m))

    def test_empty_and_decimal_sum(self):
        self.assertEqual(money.summarize([]),money.empty())
        r=commit(result([row('a',1,1,.1),row('b',1,1,.2)]),{'a':edit(3),'b':edit(3)})
        m=money.summarize(r['rows'])
        self.assertEqual(m['manual_amount'],Decimal('.9'));self.assertEqual(m['manual_delta'],Decimal('.6'))
        self.assertEqual(money.combine([m,m])['manual_delta'],Decimal('1.2'))

    def test_cache_replaced_results_restore_and_store_isolation(self):
        a=result();b=dict(result(),store='S2');host=SimpleNamespace(results={'S1':a,'S2':b})
        m=money.cached(host,a);self.assertIs(money.cached(host,a),m)
        money.cached(host,b);changed=commit(a,{'a':edit(0)});host.results['S1']=changed
        self.assertEqual(money.cached(host,changed)['manual_delta'],-24)
        self.assertEqual(money.cached(host,b)['manual_delta'],0)
        host.results['S1']=preissue.original(changed)
        self.assertEqual(money.cached(host,host.results['S1'])['manual_amount'],0)
        del host.results['S2'];money.cached(host,host.results['S1'])
        self.assertNotIn('S2',host._review_amounts_cache)

    def test_batch_total_matches_store_cent_precision(self):
        m=money.summarize([dict(new_row(price=.004),AS=1,BV=.004)])
        self.assertEqual(m['new_amount'],Decimal('0.00'))
        self.assertEqual(money.combine([m,m])['new_amount'],Decimal('0.00'))


if __name__=='__main__':unittest.main()
