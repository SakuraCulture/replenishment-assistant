"""Real isolated signing/generation/payment pipeline; no platform or live settings."""
import copy,json,sqlite3,sys,threading,unittest
from contextlib import closing
from datetime import date,timedelta
from pathlib import Path
import openpyxl
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/p) for p in ('src','tools')]
import test_jst_payment as fixtures
import payment_matching as matching,lifecycle_io as io,lifecycle_batch as batch,order_lifecycle as lc,jst_orders as jst,work_repository as wr
from runtime_support import Cancelled

class PaymentMatchingTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.DirectPayment();self.f.setUp();self.path=self.f.path;self.n=0
        self.accounts_file=self.path.parent/'jst_catalog_sync/jobs.json';self.accounts_file.parent.mkdir()
        self.account_data={'accounts':{'sd':{'warehouse':'山东仓','expectedCoid':'100','expectedUid':'1','mappingVerified':True,'expectedCompany':'合成来源甲','bindingVersion':1},'hb':{'warehouse':'河北仓','expectedCoid':'200','expectedUid':'2','mappingVerified':True,'expectedCompany':'合成来源乙','bindingVersion':1}}}
        self.accounts_file.write_text(json.dumps(self.account_data,ensure_ascii=False),'utf-8')
        self.index=matching.build(self.path);self.generation=jst.get(self.path,self.f.gids[0]);self.target=next(h for h in self.index['heads'] if h['generation_id']==self.f.gids[0])
    def tearDown(self):self.f.tearDown()
    def document(self,generation=None,internal='J1',name=None,headers=None,change=None):
        self.n+=1;g=generation or self.generation;file=self.f.root/f'paid-{self.n}.xlsx';headers=headers or io.PAYMENT_HEADERS
        book=openpyxl.Workbook();sheet=book.active;sheet.append(headers)
        for r in g['rows']:
            if matching.number(r['quantity'])=='0':continue
            value={'内部订单号':internal,'店铺名称':name if name is not None else g['head']['name'],'下单时间':g['head']['day']+' 12:00:00','商品编码':r['sku'],'商品名称':r['name'],'数量':r['quantity'],'商品金额':r['amount']}
            if change:change(value)
            sheet.append([value.get(h) for h in headers])
        book.save(file);book.close();read=batch.read([file],'payment');self.assertEqual(read['issues'],[]);self.assertEqual(len(read['documents']),1)
        return read['documents'][0]
    def plan(self,doc,index=None,scope='100',remember=True):
        index=index or matching.build(self.path);doc=copy.deepcopy(doc);doc['target']=next(h for h in index['heads'] if h['number']==self.target['number'])
        return matching.prepare(self.path,[doc],None,index,scope,remember)
    def duplicate_previous_day(self):
        import warehouse_catalog as wc
        h=self.target;day=(date.fromisoformat(h['day'])-timedelta(days=1)).isoformat();oid='cross-day-signed';parent='BH-CROSS-DAY'
        with closing(sqlite3.connect(self.path)) as db,db:
            original=db.execute('SELECT digest,saved,payload FROM normal_revisions WHERE oid=? AND revision=?',(h['oid'],h['revision'])).fetchone()
            db.execute('INSERT INTO normal_orders VALUES(?,?,?,?,?,?)',(day,h['store'],'normal_model',oid,parent,1))
            db.execute('INSERT INTO normal_revisions VALUES(?,?,?,?,?)',(oid,1,*original))
        child={**{k:h[k] for k in ('store','warehouse','name','digest')},'day':day,'oid':oid,'revision':1,'current_revision':1,'visibility_token':0,'number':parent+'-'+('HB' if h['warehouse']=='河北仓' else 'SD')}
        p=self.f.root/'cross-day-return.xlsx';w=openpyxl.Workbook();s=w.active;s.append(io.fields('return'))
        for item in self.generation['rows']:
            row={'补货单号':child['number'],'门店编码':child['store'],'供货仓':child['warehouse'],'商品编码':item['sku'],'门店确认数量':item['quantity'],'数量单位':'库存单位'}
            s.append([row.get(f) for f in io.fields('return')])
        w.save(p);w.close();info=io.inspect([p])[0];sheet=info['sheets'][0];scan=io.scan('return',[{**info,**sheet,'mapping':io.auto_mapping('return',sheet['headers'])}])
        lc.commit(self.path,lc.prepare_numbered(self.path,scan,child))
        catalog=wc.Repository(self.path.parent).catalogs()[child['warehouse']];file=self.f.root/'cross-day-stock.xlsx';w=openpyxl.Workbook();s=w.active;s.append(wc.FIELDS)
        for item in catalog['items'].values():s.append([{**item,'可用数':500}.get(f) for f in wc.FIELDS])
        w.save(file);w.close();jst.commit_refresh(self.path,jst.prepare_refresh(self.path,child['warehouse'],source=file))
        saved=jst.commit(self.path,jst.prepare(self.path,[child]));self.assertEqual(len(saved['generated']),1)
        return child
    def test_cold_warm_index_has_only_current_heads_and_no_snapshot_copies(self):
        self.assertEqual(len(self.index['heads']),4);self.assertEqual(self.index['decoded_snapshots'],6)
        self.assertEqual(matching.build(self.path)['decoded_snapshots'],0)
        self.assertTrue(all('products' not in h and 'rows' not in h for h in self.index['heads']))
        with closing(matching.Cache(self.path)) as cache,closing(sqlite3.connect(cache.path)) as db:
            self.assertTrue(all(len(row[0])<1500 for row in db.execute('SELECT payload FROM candidates')))
    def test_corrupt_disposable_index_rebuilds_without_touching_business(self):
        file=self.path.parent/'payment_matching/index-v1.sqlite3';before=self.path.read_bytes();file.write_bytes(b'broken index')
        result=matching.build(self.path);self.assertEqual(result['decoded_snapshots'],6);self.assertEqual(self.path.read_bytes(),before)
        self.assertEqual(len(list(file.parent.glob('index-v1-damaged-*.sqlite3'))),1)
    def test_forget_alias_is_explicit_cas_backed_up_and_keeps_original_order(self):
        doc=self.document(name='合成门店别名');lc.commit(self.path,self.plan(doc));alias=matching.aliases(self.path)[0]
        with self.assertRaises(wr.Conflict):matching.forget_alias(self.path,alias['key'],'stale')
        backup=matching.forget_alias(self.path,alias['key'],alias['token']);self.assertTrue(Path(backup).is_file());self.assertEqual(matching.aliases(self.path),[])
        r=matching.analyze(self.path,doc,matching.build(self.path),source_scope='100');self.assertEqual(r['state'],'已识别原内部单号')
        with self.assertRaisesRegex(ValueError,'不能删除'):matching.forget_alias(self.path,matching.mapping_key('order','100','J1'),'x')
    def test_missing_or_malformed_source_identity_never_invents_company(self):
        for value in ([],{'accounts':[None,{}]}, {'accounts':{'sd':{'mappingVerified':True,'expectedCoid':'warehouse','expectedCompany':'X'}}}):
            self.accounts_file.write_text(json.dumps(value),'utf-8');self.assertEqual(matching.accounts(self.path)['choices'],[])
        self.accounts_file.unlink();self.assertEqual(matching.accounts(self.path)['choices'],[])
    def test_blank_duplicate_and_extra_rows_are_not_complete_matches(self):
        doc=self.document();rows=doc['scan']['rows']
        for change in ('blank','duplicate','extra'):
            candidate=copy.deepcopy(doc)
            if change=='blank':candidate['scan']['rows'][0]['amount']=None
            elif change=='duplicate':candidate['scan']['rows'].append(copy.deepcopy(rows[0]))
            else:
                extra=copy.deepcopy(rows[0]);extra['sku']='EXTRA';candidate['scan']['rows'].append(extra)
            self.assertFalse(matching.analyze(self.path,candidate,self.index,source_scope='100')['automatic'])
    def test_minimum_four_headers_and_buyer_alias_preserve_raw_headers(self):
        doc=self.document(headers=['内部订单号','商品编码','数量','商品金额'])
        self.assertIsNone(doc['scan']['rows'][0]['source_order']['店铺名称'])
        doc=self.document(headers=[h.replace('买家账号','买家帐号') for h in io.PAYMENT_HEADERS])
        self.assertIn('买家帐号',doc['scan']['rows'][0]['raw_headers'])
        info={'sheets':[{'headers':io.PAYMENT_HEADERS+['买家帐号']}]}
        with self.assertRaisesRegex(ValueError,'同义表头'):io.payment_specs(info)
    def test_unique_generation_name_and_verified_company_are_required_for_automatic_selection(self):
        doc=self.document();r=matching.analyze(self.path,doc,self.index,source_scope='100')
        self.assertTrue(r['automatic']);self.assertEqual(r['candidates'][0]['number'],self.target['number'])
        self.assertFalse(matching.analyze(self.path,doc,self.index)['automatic'])
        self.assertFalse(matching.analyze(self.path,doc,self.index,source_scope='999')['automatic'])
    def test_business_alias_requires_confirmation_then_is_remembered_with_atomic_payment(self):
        doc=self.document(name='合成门店一号（别称）');r=matching.analyze(self.path,doc,self.index,source_scope='100');self.assertFalse(r['automatic'])
        before=self.f.stock();receipt=lc.commit(self.path,self.plan(doc));self.assertEqual(len(receipt),1);self.assertEqual(self.f.stock(),before)
        second=self.document(internal='J2',name='合成门店一号（别称）');r=matching.analyze(self.path,second,matching.build(self.path),source_scope='100');self.assertTrue(r['automatic'])
        r=matching.analyze(self.path,second,matching.build(self.path),source_scope='200');self.assertFalse(r['automatic'])
    def test_reimport_and_correction_use_original_internal_number_without_duplicate_version(self):
        doc=self.document();first=lc.commit(self.path,self.plan(doc))[0];index=matching.build(self.path)
        result=matching.analyze(self.path,doc,index,source_scope='100',mode='first');self.assertTrue(result['automatic']);self.assertEqual(result['state'],'已识别原内部单号')
        second=lc.commit(self.path,self.plan(doc,index))[0];self.assertFalse(second['changed']);self.assertEqual(second['version'],first['version'])
        changed=self.document(change=lambda v:v.update(商品金额='121'))
        result=matching.analyze(self.path,changed,matching.build(self.path),source_scope='100');self.assertTrue(result['automatic']);self.assertFalse(result['exact'])
    def test_same_internal_number_in_another_company_does_not_follow_first_mapping(self):
        doc=self.document();lc.commit(self.path,self.plan(doc))
        other=jst.get(self.path,self.f.gids[-1]);other_doc=self.document(other)
        result=matching.analyze(self.path,other_doc,matching.build(self.path),source_scope='200')
        self.assertTrue(result['automatic']);self.assertEqual(result['candidates'][0]['number'],other['head']['number'])
        self.assertNotEqual(result['candidates'][0]['number'],self.target['number'])
    def test_changed_global_pool_mapping_company_or_input_blocks_old_preview(self):
        doc=self.document();plan=self.plan(doc)
        self.account_data['accounts']['sd']['bindingVersion']=2;self.accounts_file.write_text(json.dumps(self.account_data),'utf-8')
        with self.assertRaisesRegex(wr.Conflict,'企业连接'):lc.commit(self.path,plan)
        self.account_data['accounts']['sd']['bindingVersion']=1;self.accounts_file.write_text(json.dumps(self.account_data),'utf-8')
        with closing(sqlite3.connect(self.path)) as db,db:
            db.execute("INSERT INTO lifecycle_mappings VALUES('payment','payment_match.concurrent','{}')")
        with self.assertRaisesRegex(wr.Conflict,'对应关系'):lc.commit(self.path,plan)
        self.assertTrue(self.f.unpaid())
    def test_missing_extra_or_changed_sku_is_not_full_match_even_with_same_totals(self):
        doc=self.document(change=lambda v:v.update(商品编码='UNRELATED'))
        result=matching.analyze(self.path,doc,self.index,source_scope='100');self.assertFalse(result['automatic']);self.assertEqual(result['exact_numbers'],[])
        doc=self.document(change=lambda v:v.update(商品金额='121'))
        self.assertFalse(matching.analyze(self.path,doc,self.index,source_scope='100')['automatic'])
    def test_cancelled_index_has_no_business_write_and_corrupt_cached_entry_is_rebuilt(self):
        before=self.path.read_bytes();stop=threading.Event();stop.set()
        with self.assertRaises(Cancelled):matching.build(self.path,cancel=stop)
        self.assertEqual(self.path.read_bytes(),before)
        cache=matching.Cache(self.path)
        with cache.db:cache.db.execute("UPDATE candidates SET payload='{}'")
        cache.close();self.assertGreater(matching.build(self.path)['decoded_snapshots'],0)
    def test_atomic_failure_does_not_leave_order_or_alias_mapping(self):
        doc=self.document(name='需要确认的新店名');plan=self.plan(doc)
        from unittest.mock import patch
        with patch('payment_matching.apply',side_effect=RuntimeError('after facts before links')):
            with self.assertRaisesRegex(RuntimeError,'before links'):lc.commit(self.path,plan)
        self.assertTrue(self.f.unpaid())
        with closing(matching.readonly(self.path)) as db:self.assertEqual(matching.mappings(db),{})
    def test_same_store_cross_day_identical_generation_cannot_hide_behind_date_or_lifecycle_priority(self):
        doc=self.document();self.duplicate_previous_day();index=matching.build(self.path)
        result=matching.analyze(self.path,doc,index,source_scope='100',mode='first')
        self.assertFalse(result['automatic']);self.assertEqual(len(result['exact_numbers']),2)
        self.assertEqual(dict(result['trace'])['完整匹配（跨全部相关日期）'],2)
    def test_new_cross_day_candidate_after_preview_blocks_atomic_commit(self):
        doc=self.document();plan=self.plan(doc);self.duplicate_previous_day()
        with self.assertRaisesRegex(wr.Conflict,'生成范围'):lc.commit(self.path,plan)
        self.assertTrue(self.f.unpaid())
    def test_deleted_original_link_cannot_rebind_or_bypass_source_scope(self):
        import archive_orders as ao
        doc=self.document();lc.commit(self.path,self.plan(doc));ao.change_many(self.path,[self.target],True)
        index=matching.build(self.path);r=matching.analyze(self.path,doc,index,source_scope='100')
        self.assertTrue(r['prevent_rebind']);self.assertEqual(r['candidates'],[])
        r=matching.analyze(self.path,doc,index);self.assertTrue(r['prevent_rebind']);self.assertEqual(r['state'],'请确认来源企业')
