"""Portable final-edit contracts. Synthetic goods; no Qt or real/user files."""
import copy
import sys
import threading
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import preissue as p
import result_semantics as sem
import allocation
from runtime_support import Cancelled

DAY='2026-10-31'


def row(sku='a',q=24,step=24,price=1,**fields):
    return dict(B='S1',E=sku,D='合成商品'+sku,AU='河北仓',G='包',BD=step,V=price,S='启用',
                H=1,I=7,J=30,K=0,L=0,N=0,AN=2,AM=1,BH=10,BN=10,AS=q,BV=q*price,
                _source='资料匹配',_meta={'blocking':[],'notes':[]},_audit={},_preview=False,
                _candidate_qty=q,_candidate_amount=q*price,_must_sell=False,**fields)


def result(rows=None,budget=None):
    rows=copy.deepcopy(rows if rows is not None else [row(),row('b',12,12,2)])
    b=allocation.apply(rows,budget) if budget is not None else {'enabled':False}
    return {'rows':rows,'store':'S1','store_name':'合成店','config':{'parameters':{'cap_days':21}},
            'summary':sem.summarize(rows),'budget':b,'sources':{'fixture':'synthetic'}}


def edit(q,**kw):return {'quantity':q,'kind':'manual','reason':'经营判断',**kw}
def listing(skus):
    import stocking_lists as sl
    value={'schema':1,'id':'synthetic','name':'合成清单','kind':'new','sources':[],
           'items':[{'sku':s} for s in skus],'defaults':{'strategy':'minimum'}}
    value['revision']=sl.revision(value);return value
def commit(r,edits,**kw):
    preview=p.preview(r,edits,DAY,**kw)
    return p.finalize(preview,[x['id'] for x in preview['risks']],current=r)


class PreissueTests(unittest.TestCase):
    def test_exact_carton_steps_and_single(self):
        for x in [0,24,48,72]:self.assertEqual(p.quantity(x,24),x)
        for x in range(10):self.assertEqual(p.quantity(x,1),x)
    def test_keyboard_paste_invalid_never_rounds(self):
        for value in [23,25,24.000001,-24,'',None,True,'=24','nan','Infinity','1e999']:
            with self.subTest(value=value),self.assertRaises(ValueError):p.quantity(value,24)
    def test_empty_is_not_zero(self):
        with self.assertRaises(ValueError):p.quantity('',24)
        self.assertEqual(p.quantity('0',24),0)
    def test_nonmutation_and_other_row_exact(self):
        r=result();old=copy.deepcopy(r);after=commit(r,{'a':edit(48)})
        self.assertEqual(r,old);self.assertEqual(after['rows'][1],old['rows'][1])
        self.assertEqual(sem.order_quantity(after['rows'][0]),48)
        self.assertEqual(after['rows'][0]['AS'],24);self.assertEqual(after['summary']['amount'],72)
    def test_restore_including_key_order(self):
        r=result(budget=30);after=commit(r,{'a':edit(0)})
        original=p.original(after)
        self.assertEqual(original,r)
        for a,b in zip(original['rows'],r['rows']):self.assertEqual(list(a),list(b))
        self.assertEqual(commit(after,{}),r)
    def test_zero_requires_specific_risk_and_is_final(self):
        r=result();v=p.preview(r,{'a':edit(0)},DAY)
        self.assertEqual([x['kind'] for x in v['risks']],['zero'])
        with self.assertRaises(ValueError):p.finalize(v)
        after=p.finalize(v,[x['id'] for x in v['risks']]);p.validate_result(after,DAY)
        self.assertEqual(sem.order_quantity(after['rows'][0]),0)
    def test_reason_required(self):
        with self.assertRaisesRegex(ValueError,'原因'):p.preview(result(),{'a':edit(48,reason='')},DAY)
    def test_excluded_and_local_and_unknown_cannot_override(self):
        for field,value in [('S','禁用'),('_source','地采商品'),('V',None),('BD',0),('K',None)]:
            with self.subTest(field=field):
                d=row();d[field]=value
                with self.assertRaises(ValueError):p.preview(result([d]),{'a':edit(24)},DAY)
    def test_stopped_cannot_override(self):
        d=row();d['_meta']['export_status']='停采'
        with self.assertRaisesRegex(ValueError,'停采'):p.preview(result([d]),{'a':edit(24)},DAY)
    def test_cap_scoped_confirmation(self):
        r=result();r['rows'][0].update(BH=1,BN=1)
        v=p.preview(r,{'a':edit(48)},DAY)
        self.assertIn('cap',[x['kind'] for x in v['risks']])
        after=p.finalize(v,[x['id'] for x in v['risks']]);p.validate_result(after,DAY)
        self.assertEqual(after['rows'][1],r['rows'][1])
    def test_cap_disabled_and_not_forecast_days(self):
        r=result();r['rows'][0].update(BH=1,BN=1);r['config']['parameters']['cap_days']=0
        self.assertNotIn('cap',[x['kind'] for x in p.preview(r,{'a':edit(48)},DAY)['risks']])
        r['config']['parameters']['cap_days']=14
        self.assertIn('cap',[x['kind'] for x in p.preview(r,{'a':edit(48)},DAY)['risks']])
    def test_original_automatic_exception_not_reblocked(self):
        r=result();r['rows'][0].update(BH=0,BN=0)
        self.assertNotIn('cap',[x['kind'] for x in p.preview(r,{'a':edit(0)},DAY)['risks']])
    def test_unknown_forecast_requires_confirmation(self):
        r=result();r['rows'][0].update(AN=0,BN=0,BH=0)
        self.assertIn('unknown',[x['kind'] for x in p.preview(r,{'a':edit(48)},DAY)['risks']])
    def test_stale_confirmation_quantity_and_source(self):
        r=result();r['rows'][0].update(BH=0,BN=0);v=p.preview(r,{'a':edit(48)},DAY)
        v2=p.preview(r,{'a':edit(72)},DAY)
        with self.assertRaises(ValueError):p.finalize(v2,[x['id'] for x in v['risks']])
        modified=copy.deepcopy(r);modified['rows'][0]['V']=2
        with self.assertRaises(ValueError):p.finalize(v,[x['id'] for x in v['risks']],current=modified)
    def test_archive_guard_rejects_tamper(self):
        after=commit(result(),{'a':edit(48)})
        for mutate in [lambda x:x['rows'][0].update(_execution_qty=72),lambda x:x['preissue'].update(confirmed=[]),lambda x:x['rows'][0].update(V=3)]:
            x=copy.deepcopy(after);mutate(x)
            if x==after:continue
            with self.assertRaises(ValueError):p.validate_result(x,DAY)
    def test_pending_and_cross_day_block_archive(self):
        after=commit(result(),{'a':edit(48)})
        with self.assertRaises(ValueError):p.validate_result(after,'2026-11-01')
        with self.assertRaises(ValueError):p.validate_payload({'day':DAY,'results':{'S1':after},'preissue_drafts':{'S1':{}}},['S1'])
    def test_zero_budget_never_reallocates(self):
        r=result(budget=0);v=p.preview(r,{'a':edit(24)},DAY)
        self.assertTrue(v['errors'])
        with self.assertRaises(ValueError):p.finalize(v,[x['id'] for x in v['risks']])
    def test_one_extra_and_five_percent(self):
        r=result([row('a',20,1,5)],100);v=p.preview(r,{'a':edit(21)},DAY)
        self.assertFalse(v['errors']);self.assertIn('budget',[x['kind'] for x in v['risks']])
        after=p.finalize(v,[x['id'] for x in v['risks']]);self.assertEqual(after['budget']['spent'],105)
        self.assertTrue(p.preview(r,{'a':edit(22)},DAY)['errors'])
    def test_money_precision_does_not_become_quantity_error(self):
        r=result([row('a',24,24,0.12345678901234567)])
        after=commit(r,{'a':edit(48)})
        # The source contract is an existing Python float (repr ends in 566),
        # not the higher-precision decimal spelling of the literal above.
        self.assertEqual(str(r['rows'][0]['V']),'0.12345678901234566')
        self.assertEqual(after['rows'][0]['_preissue']['amount_exact'],'5.92592587259259168')
        p.validate_result(after,DAY)
    def test_two_units_cannot_borrow_old_expensive_unit(self):
        r=result([row('base',1,1,100),row('extra',0,1,2)],100)
        self.assertTrue(p.preview(r,{'extra':edit(2)},DAY)['errors'])
    def test_both_warehouses_and_hidden_rows_share_budget(self):
        a=row('a',1,1,60);b=row('b',1,1,40);b['AU']='山东仓';r=result([a,b],100)
        self.assertTrue(p.preview(r,{'b':edit(2)},DAY)['errors'])
    def test_released_budget_not_redistributed(self):
        r=result(budget=30);q=sem.order_quantity(r['rows'][1]);after=commit(r,{'a':edit(0)})
        self.assertEqual(sem.order_quantity(after['rows'][1]),q)
    def test_minimum_existing_stock_and_inbound(self):
        for k,l in [(3,0),(0,8)]:
            d=row();d.update(K=k,L=l);self.assertEqual(p.target_quantity(d,'minimum'),0)
        self.assertEqual(p.target_quantity(row(),'minimum'),24)
    def test_target_gap_not_added_to_current_quantity(self):
        d=row(q=4,step=1);d.update(K=3,L=2);self.assertEqual(p.target_quantity(d,'target',12),7)
        d['BD']=6;self.assertEqual(p.target_quantity(d,'target',12),12)
    def test_list_repeat_and_manual_zero_preservation(self):
        r=result();after=commit(r,{'a':edit(0)});items=listing(['a'])
        draft=p.list_edits(after,items,'minimum');self.assertEqual(draft['edits']['a']['quantity'],0);self.assertEqual(len(draft['skipped']),1)
        draft=p.list_edits(after,items,'minimum',replace_manual=True);self.assertEqual(draft['edits']['a']['quantity'],24)
        v=commit(after,draft['edits'],lists=draft['lists']);again=p.list_edits(v,items,'minimum')
        self.assertEqual(again['edits']['a']['quantity'],24)
    def test_list_missing_is_not_zero(self):
        draft=p.list_edits(result(),listing(['missing']),'minimum');self.assertEqual(draft['edits'],{});self.assertEqual(len(draft['pending']),1)
    def test_new_group_ignores_inbound_and_stockout_not_raw_missing(self):
        d=row(q=0);d.update(J=0,K=0,L=8,N=10)
        self.assertTrue(p.current_month(d));d['J']=None;self.assertFalse(p.current_month(d))
    def test_identity_unique_and_cancel_no_mutation(self):
        r=result([row(),row()])
        with self.assertRaises(ValueError):p.preview(r,{},DAY)
        r=result();cancel=threading.Event();cancel.set();before=copy.deepcopy(r)
        with self.assertRaises(Cancelled):p.preview(r,{'a':edit(48)},DAY,cancel=cancel)
        self.assertEqual(r,before)
    def test_legacy_identity(self):
        r=result();p.validate_result(r,DAY);self.assertIs(p.preview(r,{},DAY)['result'],r)


if __name__=='__main__':unittest.main()
