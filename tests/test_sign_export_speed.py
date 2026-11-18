"""Canonical bytes, bounded immutable-key costs and complete merchant styles."""
import copy
import hashlib
import tempfile
import unittest
from datetime import date,datetime
from pathlib import Path
from unittest.mock import patch

import openpyxl
from openpyxl.styles import Font
from openpyxl.styles.styleable import StyleDescriptor
import encoding_store as spool
import normal_exports as export
import work_repository as wr
import test_adaptive_resources as adaptive
from test_archive_orders import seed


class EncodingCosts(unittest.TestCase):
    def canonical(self,value,**kwargs):
        parts=list(spool.json_bytes(value,**kwargs))
        self.assertTrue(all(len(p)<=spool.BUFFER for p in parts))
        self.assertEqual(b''.join(parts),wr.dumps(value).encode('utf-8'))

    def test_repeated_schema_unicode_dates_and_numeric_edges_keep_bytes(self):
        rows=[{'sku':f'{i:013d}','quantity':i%7,'price':-0.0 if i%2 else 1.25,
               'name':'=文字😀汉字\\\"\n'+str(i),'notes':{'date':date(2026,11,17),'at':datetime(2026,11,17,1,2)},
               'valid':True,'missing':None} for i in range(600)]
        before=copy.deepcopy(rows)
        for limit in (0,64,spool.BUFFER,8*1024**2):self.canonical({'rows':rows},fast_limit=limit)
        self.assertEqual(rows,before)

    def test_key_costs_keep_original_conservative_estimate(self):
        probe=spool._KeyCosts()
        for keys in [('A','B','名称','sku'),tuple('field'+str(n) for n in range(90)),tuple(range(8)),('long'*100,'B','C','D')]:
            value=dict.fromkeys(keys,0)
            expected=4*len(value)+2+sum(6*len(k)+4 if isinstance(k,str) else 128 for k in value)
            self.assertEqual(probe.cost(value),expected);self.assertEqual(probe.cost(value),expected)

    def test_equal_key_structure_never_caches_changed_values(self):
        probe=spool._KeyCosts();value={'a':'x','b':1,'c':True,'d':None}
        self.assertTrue(spool._small(value,[256,8192],probe));self.assertTrue(probe.costs)
        value['a']='😀'*10000
        self.assertFalse(spool._small(value,[256,8192],probe))
        self.canonical(value)

    def test_cached_structures_keep_byte_and_node_limits(self):
        probe=spool._KeyCosts()
        values=[{'a':'汉字😀'*n,'b':-0.0,'c':n,'d':{'at':date(2026,11,17)}} for n in (0,1,19,200)]
        for value in values:
            for limit in (0,64,256,8192):
                if spool._small(value,[limit,8192],probe):
                    self.assertLessEqual(len(wr.dumps(value).encode()),limit)
        self.assertFalse(spool._small([dict.fromkeys(('a','b','c','d'),0)]*9000,[2**28,8192],probe))

    def test_cache_retention_is_bounded_and_does_not_grow_after_capacity(self):
        probe=spool._KeyCosts()
        for n in range(2000):probe.cost({f'field{n}_{i}':0 for i in range(5)})
        self.assertLessEqual(probe.bytes,spool.KEY_CACHE_BYTES)
        self.assertLessEqual(len(probe.costs),spool.KEY_CACHE_ENTRIES)
        self.assertTrue(probe.costs)

    def test_each_encoding_owns_its_cache(self):
        made=[];factory=spool._KeyCosts
        def create():
            p=factory();made.append(p);return p
        value={'a':0,'b':1,'c':2,'d':3}
        with patch.object(spool,'_KeyCosts',side_effect=create):
            self.canonical(value);value['a']=999;self.canonical(value)
        self.assertEqual(len(made),2);self.assertIsNot(made[0],made[1])

    def test_complex_keys_are_not_hashed_by_optional_cache(self):
        class Key:
            calls=0
            def __hash__(self):self.calls+=1;return 19
        key=Key();value={key:0,1:1,2:2,3:3};before=key.calls
        spool._KeyCosts().cost(value)
        self.assertEqual(key.calls,before)

    def test_cache_allocation_failure_uses_original_calculation(self):
        class Full(dict):
            def __setitem__(self,*_):raise MemoryError('synthetic cache pressure')
        probe=spool._KeyCosts();probe.costs=Full();value=dict.fromkeys(('a','b','c','d'),0)
        self.assertEqual(probe.cost(value),4*4+2+4*(6+4))
        self.assertTrue(probe.disabled);self.assertEqual(probe.bytes,0)
        with patch.object(spool,'_KeyCosts',side_effect=MemoryError('synthetic constructor pressure')):self.canonical(value)

    def test_streaming_fallback_does_not_repeat_size_probe(self):
        with patch.object(spool,'_small',side_effect=AssertionError('streaming does not need fast-size proof')):
            self.canonical({'rows':[{'a':1,'b':'😀'*30000,'c':True,'d':None}]},fast_limit=0)

    def test_fast_encoder_pressure_emits_exactly_one_canonical_document(self):
        original=wr.dumps;value={'rows':[{'a':1,'b':2,'c':3,'d':'x'}]*40};pressures=[];failed=[False]
        def fail_once(obj):
            if obj is value and not failed[0]:failed[0]=True;raise MemoryError('synthetic fast encoder pressure')
            return original(obj)
        with patch.object(wr,'dumps',side_effect=fail_once):
            raw=b''.join(spool.json_bytes(value,on_pressure=lambda:pressures.append(True)))
        self.assertEqual(raw,original(value).encode());self.assertEqual(pressures,[True])

    def test_lower_bound_only_rejects_oversized_containers(self):
        probe=spool._KeyCosts()
        with patch.object(probe,'cost',side_effect=AssertionError('oversized container should not scan keys')):
            self.assertFalse(spool._small(dict.fromkeys(range(1000),0),[64,8192],probe))
        self.assertFalse(spool._small([0]*10000,[64,8192],probe))
        self.canonical({n:n for n in range(12)})

    def test_invalid_values_remain_rejected(self):
        for value in (float('nan'),float('inf'),object()):
            for limit in (0,spool.BUFFER):
                with self.subTest(value=type(value).__name__,limit=limit),self.assertRaises((TypeError,ValueError)):
                    b''.join(spool.json_bytes({'a':1,'b':2,'c':3,'d':value},fast_limit=limit))


class MerchantStyles(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.work,self.repo,self.payload=seed(self.root);self.order=self.repo.get(wr.today(),'T')
        base=self.order['payload']['result']['rows'][0];rows=[]
        for i in range(40):
            row=copy.deepcopy(base);row.update(E=f'{i:013d}',F='0000000000001',D='=保留文字'+str(i))
            if i%7==0:row.update(_budget_active=True,_allocated_qty=0,_allocated_amount=0)
            rows.append(row)
        self.order['payload']['result']['rows']=rows

    def test_memory_and_streaming_export_keep_formats_text_comments_and_identity(self):
        source=adaptive.Source();a=self.root/'memory.xlsx';b=self.root/'stream.xlsx';before=copy.deepcopy(self.order)
        export.export(self.order,a,warehouse='河北仓',resources=source.budget());source.tight()
        export.export(self.order,b,warehouse='河北仓',resources=source.budget())
        self.assertEqual(adaptive.workbook_facts(a),adaptive.workbook_facts(b));self.assertEqual(self.order,before)

    def test_each_workbook_owns_font_indices(self):
        source=adaptive.Source();a=self.root/'one.xlsx';b=self.root/'two.xlsx';factory=openpyxl.Workbook
        export.export(self.order,a,warehouse='河北仓',resources=source.budget())
        def prepopulated():
            book=factory();book._fonts.add(Font(name='synthetic earlier font',bold=True));return book
        with patch.object(export,'Workbook',side_effect=prepopulated):export.export(self.order,b,warehouse='河北仓',resources=source.budget())
        self.assertEqual(adaptive.workbook_facts(a),adaptive.workbook_facts(b))

    def test_existing_date_format_is_not_replaced_by_plain_style(self):
        rows=self.order['payload']['result']['rows']
        # This test needs all three rows in the sell-through sheet. Zero-budget
        # fixtures are deliberately omitted by the unchanged business rules.
        for row in rows:row['_budget_active']=False
        rows[0]['D']=date(2026,11,17);rows[2]['D']=datetime(2026,11,17,1,2)
        path=self.root/'dates.xlsx';export.export(self.order,path,warehouse='河北仓',resources=adaptive.Source().budget())
        book=openpyxl.load_workbook(path)
        try:
            sheet=book['中心仓动销补货清单']
            self.assertEqual(sheet.cell(2,4).number_format,'yyyy-mm-dd')
            self.assertEqual(sheet.cell(3,4).number_format,'General')
            self.assertEqual(sheet.cell(4,4).number_format,'yyyy-mm-dd h:mm:ss')
            self.assertEqual(sheet.cell(3,5).value,'0000000000001')
            self.assertEqual(sheet.cell(3,4).data_type,'s')
        finally:book.close()

    def test_font_registration_is_bounded_by_styles_not_cells(self):
        original=StyleDescriptor.__set__;calls=[]
        def observed(descriptor,*args):
            if descriptor.collection=='_fonts':calls.append(True)
            return original(descriptor,*args)
        with patch.object(StyleDescriptor,'__set__',observed):
            export.export(self.order,self.root/'registered.xlsx',warehouse='河北仓',resources=adaptive.Source().budget())
        self.assertLess(len(calls),20)


if __name__=='__main__':unittest.main()
