"""Warehouse-anchored newness regressions using ordinary and archive source files."""
from contextlib import closing
from datetime import date,datetime,timedelta
from hashlib import sha256
from pathlib import Path
import tempfile
import unittest

import openpyxl
import batch_service
import catalog_sync_validation
import engine
import first_fill
import new_product_focus as focus
import new_products
import preissue
import restock_policy
import result_semantics as sem
import review_filters
from table_records import bridge_table
import warehouse_catalog as wc
import warehouse_newness as recent


DAY='2026-11-22'
CODE='0000517821362000007'
FIELDS=wc.ARCHIVE_REQUIRED+[recent.FIRST_INBOUND,'标准装箱数量','新店新品首铺数量','订单占有数','虚拟分类']


def archive_rows(entries):
    return [{'商品编码':key,'商品名':str(key)+' 测试商品','产品分类':'饮品','启用':status,
             '基本售价':2,'出库箱规':6,'可用数':stock,'主仓实际库存':stock+2,
             '商品创建日期':created,'商品标签':tag,'首次入库时间':first,'标准装箱数量':24,
             '新店新品首铺数量':fill,'订单占有数':2}
            for key,first,status,stock,tag,created,fill in entries]


def write(path,headers,rows):
    book=openpyxl.Workbook()
    book.active.append(headers)
    for row in rows:book.active.append([row.get(h) for h in headers])
    book.save(path)
    book.close()
    return path


def sale(code=CODE,**changes):
    return {'仓库/门店编码':'S1','仓库/门店名称':'隔离店','商品编码':code,'商品名称':'原销售商品',
            '日销':0,'周销':0,'月销':0,'可用库存':0,'在途库存':0,'缺货天数（天）':0,'补货状态':'正常',
            **changes}


def calculate(catalogs,primary,secondary=None,*,day=DAY,profile=None,sales=None,enabled=True):
    lines=sales or [sale()]
    load=wc.prepare_records(bridge_table(list(lines[0]),lines,'来源'),catalogs)
    load['config']=restock_policy.patch(load['config'],{'new_stocking':enabled})
    settings={'warehouses':engine.DEFAULT_WAREHOUSES,'exception':False,'budget':None,
              'supply_route':{'primary':primary,'secondary':secondary}}
    return batch_service.calculate_store(load,'S1',new_products.calculation_settings(settings,profile,day),None)


def monthly(codes):
    value=focus.empty('2026-11')
    value['items']=[{'sku':code,'focused':True} for code in codes]
    value['revision']=focus.revision(value)
    return value


class ArchiveCatalog(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)
    def import_file(self,warehouse,entries,headers=FIELDS):
        path=write(self.root/(warehouse+'.xlsx'),headers,archive_rows(entries))
        return wc.import_catalog(path,warehouse)
    def test_realistic_spreadsheet_and_full_archive_boundaries(self):
        entries=[
            (CODE,date(2026,10,8),'备用',0,'必卖清单',date(2026,10,9),18),
            ('@today','2026/11/22 0:00:00','启用',0,'',None,6),
            ('@expired','2026-10-07','启用',6,'',None,6),
            ('@future',datetime(2026,11,23,14),'启用',4,'',None,6),
            ('@invalid','2026/02/30 00:00:00','启用',4,'',None,6),
            ('@empty',None,'禁用',10,'',None,6),
            ('@new-unknown-before','2026-11-01','启用',0,'',None,6),
        ]
        rows=archive_rows(entries)
        rows[0]['产品分类']='分类-未设置'
        rows.append({'商品编码':'@pending','启用':'备用','可用数':0})
        rows.append({'商品编码':'@pending-stock','启用':'备用','可用数':10,'主仓实际库存':10})
        path=write(self.root/'商品主题分析_全商品档案_20261122223244_211511244_1.xlsx',FIELDS,rows)
        data=wc.import_catalog(path,'河北仓')
        self.assertEqual(data['source_rows'],9)
        self.assertEqual(data['quarantined'][0]['商品编码'],'@pending')
        self.assertEqual(data['quarantined'][0]['reason'],wc.ORPHAN_REASON)
        pending=data['quarantined'][1]
        self.assertEqual((pending['商品编码'],pending['可用数'],pending['reason']),('@pending-stock',10,wc.ORPHAN_STOCK_REASON))
        self.assertEqual(data['source_observation']['taskId'],'211511244')
        self.assertEqual(list(data['items']),[e[0] for e in entries])
        first=data['items'][CODE]
        self.assertIsNone(first['分类'])
        self.assertEqual((first['可用数'],first['实际库存数'],first['出库箱规'],first['标准装箱数量'],first[first_fill.FIELD]),(0,2,6,24,18))
        self.assertEqual(first['主仓实际库存'],2)
        self.assertEqual(first['商品标签'],'必卖清单')
        view={r['商品编码']:b for r,b in recent.view(data,DAY)}
        self.assertEqual([k for k,v in view.items() if v['new']],[CODE,'@today','@new-unknown-before'])
        self.assertEqual(view[CODE]['age'],45)
        self.assertEqual(view[CODE]['warning'],recent.EARLY)
        self.assertEqual(view['@today']['age'],0)
        self.assertEqual(view['@expired']['age'],46)
        self.assertEqual(view['@future']['issue'],recent.FUTURE)
        self.assertEqual(view['@invalid']['issue'],recent.INVALID)
        self.assertEqual(view['@empty']['issue'],recent.UNKNOWN)
        self.assertFalse(recent.decide(recent.source(data,first),'2026-11-23')['new'])
        repo=wc.Repository(self.root)
        repo.save(data)
        self.assertEqual(repo.catalogs()['河北仓'],data)
        self.assertEqual(sha256(path.read_bytes()).hexdigest(),data['source_sha256'])

    def test_legacy_missing_whole_date_column_and_raw_code_validation(self):
        entries=[(CODE,None,'启用',0,'',None,18)]
        missing=self.import_file('河北仓',entries,[x for x in FIELDS if x!=recent.FIRST_INBOUND])
        self.assertIsNone(recent.view(missing,DAY))
        result=calculate({'河北仓':missing},'河北仓')
        with self.assertRaisesRegex(ValueError,'关注清单'):focus.require_for(None,DAY,[result])
        with self.assertRaisesRegex(ValueError,'须保留平台原文本编码'):
            self.import_file('河北仓',[(100,None,'启用',0,'',None,18)])
        with self.assertRaisesRegex(ValueError,'商品编码重复'):
            self.import_file('河北仓',entries*2)

    def test_bound_warehouse_and_retained_raw_count(self):
        entries=[(CODE,'2026-10-08','启用',1,'',None,18)]
        path=write(self.root/'archive.xlsx',FIELDS,archive_rows(entries)+[{'商品编码':'@pending','启用':'备用','可用数':0}])
        receipt={'kind':'jst_full_archive','warehouse':'河北仓','reportWarehouse':'优团河北石家庄仓','company':'优团河北石家庄仓',
                 'expectedRows':2,'taskId':'211511244','queriedAt':'2026-11-22T22:32:00+08:00','exportedAt':'2026-11-22T22:32:44+08:00',
                 'query':{'sales':'unlimited','enabled':'all','excludeZero':False,'excludeCancelledBeforeShipment':True}}
        request={'schema':1,'file':str(path),'warehouse':'河北仓','expected_rows':2,'sha256':catalog_sync_validation.digest(path),
                 'candidate':str(self.root/'catalog.json'),'evidence':receipt}
        parsed=catalog_sync_validation.validate(request)
        self.assertEqual((parsed['raw_rows'],parsed['rows'],parsed['quarantined']),(2,1,1))
        data=catalog_sync_validation.candidate(parsed)
        self.assertEqual(data['source_observation']['taskId'],'211511244')
        default={**receipt,'reportScope':'account_default'};del default['reportWarehouse']
        default_request={**request,'evidence':default,'candidate':str(self.root/'default.json')}
        observed=catalog_sync_validation.candidate(catalog_sync_validation.validate(default_request))['source_observation']
        self.assertEqual(observed['reportScope'],'account_default')
        self.assertNotIn('reportWarehouse',observed)
        for bad in ({**receipt,'warehouse':'山东仓'},{**receipt,'query':{**receipt['query'],'excludeZero':True}},
                    {**receipt,'reportScope':'account_default'},{**default,'reportScope':'unknown'}):
            with self.assertRaisesRegex(ValueError,'未通过核验'):
                catalog_sync_validation.validate({**request,'evidence':bad})

    def test_final_warehouse_drives_existing_store_eligibility_and_signature(self):
        hb=self.import_file('河北仓',[(CODE,'2026-10-08','启用',100,'',None,18),
                                      ('@not-sold','2026-11-22','启用',0,'',None,30)])
        sd=self.import_file('山东仓',[(CODE,'2026-10-07','启用',0,'',None,30)])
        both={'河北仓':hb,'山东仓':sd}
        hb_run=calculate(both,'山东仓','河北仓')
        hb_row=hb_run['rows'][0]
        self.assertEqual([d['E'] for d in hb_run['rows']],[CODE])
        self.assertEqual((hb_row['AU'],sem.order_quantity(hb_row),hb_row['AS']),('河北仓',18,0))
        focus.require_for(None,DAY,[hb_run])
        self.assertEqual(next(sem.signed_rows(hb_run['rows'],None))[0],'本月新品')
        self.assertEqual(review_filters.group(hb_row,known=False),'本月新品')
        sd_run=calculate(both,'山东仓',profile=monthly([CODE]))
        sd_row=sd_run['rows'][0]
        self.assertEqual((sd_row['AU'],sem.order_quantity(sd_row)),('山东仓',0))
        self.assertEqual(review_filters.group(sd_row,{CODE}),'往期新品')
        self.assertFalse(preissue.current_month(sd_row))
        self.assertTrue(preissue.current_month(hb_row))
        for variant in (calculate(both,'河北仓',day='2026-11-23'),calculate(both,'河北仓',enabled=False),
                        calculate(both,'河北仓',sales=[sale(在途库存=6)])):
            self.assertEqual(len(variant['rows']),1)
            self.assertNotIn('_new_requested',variant['rows'][0])
        self.assertEqual(hb_row['_meta']['warehouse_newness']['day'],DAY)
        self.assertEqual(hb_row['_meta']['warehouse_newness']['catalog_revision'],hb['revision'])

    def test_scheduled_workbench_preserves_run_day_and_warehouse_basis(self):
        import scheduled_execution as execution
        import scheduled_replenishment as scheduler
        from test_scheduled_replenishment import LocalBackend,fixture

        work,plan,_,_,manifest=fixture(self.root)
        catalogs={
            '河北仓':self.import_file('河北仓',[(CODE,'2026-10-08','启用',100,'',None,18)]),
            '山东仓':self.import_file('山东仓',[(CODE,'2026-10-07','启用',100,'',None,24)]),
        }
        jobs=scheduler.Repository(work.path,lambda:clock)
        for file in manifest['files']:
            code=file['warehouse_code']
            row=sale(**{'仓库/门店编码':code,'仓库/门店名称':'测试'+code})
            path=write(Path(file['file']),list(row),[row])
            file['sha256']=sha256(path.read_bytes()).hexdigest()
        clock=datetime(2026,11,22,6,30,tzinfo=scheduler.TZ)
        basis=execution.freeze(self.root,plan,DAY)
        plan['check_signature']=execution.check_signature(basis)
        day_results=[]
        for offset in (0,1):
            run=jobs.manual(plan)
            execution.Runner(jobs,LocalBackend(catalogs,manifest),self.root).execute(run['id'])
            completed=jobs.get(run['id'])
            self.assertEqual(completed['state'],'COMPLETE',completed['message'])
            record,frozen=execution.workbench_record(jobs,run['id'])
            rows={store:result['rows'][0] for store,result in record['payload']['results'].items()}
            self.assertEqual({s:v['warehouse_new_day'] for s,v in frozen['jobs'].items()},
                             dict.fromkeys(rows,clock.date().isoformat()))
            for row in rows.values():
                mark=row['_meta']['warehouse_newness']
                self.assertEqual((mark['warehouse'],mark['day']),(row['AU'],clock.date().isoformat()))
            day_results.append([sem.order_quantity(rows[code]) for code in ('A','B')])
            clock+=timedelta(days=1)
        self.assertEqual(day_results,[[18,0],[0,0]])

    def test_screening_uses_the_supply_warehouse_for_lists_and_first_fill(self):
        hb_rows=archive_rows([
            (CODE,DAY,'启用',30,'',None,18),
            ('@disabled',DAY,'禁用',30,'',None,18),
            ('@standby-zero',DAY,'备用',0,'',None,18),
            ('@other-category',DAY,'启用',30,'',None,18),
        ])
        hb_rows[0]['虚拟分类']='其他分类，标杆淘汰（切临沂仓发货）'
        hb_rows[3]['虚拟分类']='普通关注'
        hb=wc.import_catalog(write(self.root/'hb.xlsx',FIELDS,hb_rows),'河北仓')
        sd=self.import_file('山东仓',[(CODE,DAY,'启用',30,'',None,24)])
        both={'河北仓':hb,'山东仓':sd}
        visible={item['商品编码'] for item,basis in recent.view(hb,DAY) if basis['new']}
        self.assertEqual(visible,{'@standby-zero','@other-category'})
        self.assertEqual(hb['items'][CODE]['虚拟分类'],hb_rows[0]['虚拟分类'])
        sales=[sale(r['商品编码']) for r in hb_rows]
        profile=monthly([CODE,'@disabled'])
        result=calculate(both,'河北仓',profile=profile,sales=sales)
        self.assertEqual([d['E'] for d in result['rows']],[r['商品编码'] for r in hb_rows])
        retired,disabled,standby,ordinary=result['rows']
        self.assertEqual(recent.for_row(retired)['excluded_reason'],recent.RETIRED)
        self.assertEqual(recent.for_row(disabled)['excluded_reason'],recent.DISABLED)
        self.assertNotIn('_new_requested',retired)
        self.assertNotIn('_new_requested',disabled)
        self.assertEqual([standby['_new_requested'],ordinary['_new_requested']],[18,18])
        self.assertEqual([row['E'] for _,row in sem.signed_rows(result['rows'],profile)],
                         ['@standby-zero','@other-category'])
        self.assertFalse(preissue.current_month(retired))
        with self.assertRaisesRegex(ValueError,'淘汰品'):
            new_products.apply(new_products.original(result),{CODE:6})
        with self.assertRaisesRegex(ValueError,'淘汰品'):
            restock_policy.guard_new(result,retired,'new',6)
        swapped=calculate(both,'山东仓','河北仓',profile=profile)['rows'][0]
        self.assertEqual((swapped['AU'],swapped['_new_requested']),('山东仓',24))

    def test_store_consumables_are_screened_by_the_supply_warehouse_category(self):
        hb_rows=archive_rows([
            (CODE,DAY,'启用',30,'',None,18),
            ('@named-consumable',DAY,'启用',30,'门店耗材',None,18),
        ])
        hb_rows[0]['产品分类']=' 门店耗材 '
        hb_rows[1]['商品名']='名称中有门店耗材的测试商品'
        hb=wc.import_catalog(write(self.root/'hb_category.xlsx',FIELDS,hb_rows),'河北仓')
        sd=self.import_file('山东仓',[(CODE,DAY,'启用',30,'',None,24)])
        catalogs={'河北仓':hb,'山东仓':sd}
        sales=[sale(r['商品编码']) for r in hb_rows]
        profile=monthly([CODE])

        basis={item['商品编码']:value for item,value in recent.view(hb,DAY)}
        self.assertEqual({sku for sku,value in basis.items() if value['new']},{'@named-consumable'})
        self.assertEqual(basis[CODE]['excluded_reason'],'仓商品分类为门店耗材')
        self.assertIn(CODE,hb['items'])
        result=calculate(catalogs,'河北仓','山东仓',sales=sales,profile=profile)
        excluded,ordinary=result['rows']
        self.assertEqual((excluded['AU'],sem.order_quantity(excluded)),('河北仓',0))
        self.assertEqual(recent.for_row(excluded)['excluded_reason'],basis[CODE]['excluded_reason'])
        self.assertEqual((ordinary['E'],ordinary['_new_requested']),('@named-consumable',18))
        self.assertEqual([(group,row['E']) for group,row in sem.signed_rows(result['rows'],profile)],
                         [('本月新品','@named-consumable')])

        swapped=calculate(catalogs,'山东仓','河北仓',sales=sales,profile=profile)['rows'][0]
        self.assertEqual((swapped['AU'],swapped['_new_requested']),('山东仓',24))
        for previous_rule in ('warehouse-first-inbound-45d-v1','warehouse-first-inbound-45d-v2'):
            saved={**recent.for_row(swapped),'rule':previous_rule}
            row={**swapped,'_meta':{**swapped['_meta'],'warehouse_newness':saved}}
            self.assertEqual(recent.for_row(row),saved)
            self.assertEqual(sem.new_product_group(row),'本月新品')

    def test_old_archive_without_virtual_category_waits_for_refresh(self):
        unloaded=self.import_file('河北仓',[(CODE,DAY,'启用',30,'',None,18)],
                                  [field for field in FIELDS if field!='虚拟分类'])
        self.assertNotIn('虚拟分类',unloaded['items'][CODE])
        basis=recent.view(unloaded,DAY)[0][1]
        self.assertFalse(basis['new'])
        self.assertEqual(basis['screening_issue'],recent.MISSING_VIRTUAL_CATEGORY)
        profile=monthly([CODE])
        result=calculate({'河北仓':unloaded},'河北仓',profile=profile)
        self.assertEqual(sem.order_quantity(result['rows'][0]),0)
        self.assertEqual(list(sem.signed_rows(result['rows'],profile)),[])
        focus.require_for(None,DAY,[result])

    def test_new_warehouse_detail_export_preserves_rows_types_and_previous_file(self):
        from warehouse_new_exports import HEADERS,export_detail

        rows=[(CODE,'=原样商品名','2026-10-08',45,'备用',0,'',3),
              ('@004','原样第二款',DAY,0,'启用',12.5,'仓内记录',8)]
        dest=self.root/'新品明细'
        path=export_detail(dest,rows,'河北仓',DAY,'0012345678abcdef')
        self.assertEqual(path,dest.with_suffix('.xlsx'))
        with closing(openpyxl.load_workbook(path)) as book:
            sheet=book['新品明细']
            self.assertEqual(tuple(c.value for c in sheet[1]),HEADERS)
            self.assertEqual([[c.value for c in line] for line in sheet.iter_rows(min_row=2)], [
                [CODE,'=原样商品名',datetime(2026,10,8),45,'备用',0,None,3,'河北仓',datetime(2026,11,22),'0012345678abcdef'],
                ['@004','原样第二款',datetime(2026,11,22),0,'启用',12.5,'仓内记录',8,'河北仓',datetime(2026,11,22),'0012345678abcdef'],
            ])
            self.assertEqual((sheet['A2'].data_type,sheet['B2'].data_type,sheet['F2'].data_type),('s','s','n'))
            self.assertEqual(sheet.auto_filter.ref,'A1:K3')
            self.assertEqual(sheet.freeze_panes,'C2')
        original=path.read_bytes()
        with self.assertRaises(FileExistsError):
            export_detail(dest,rows,'山东仓',DAY,'changed')
        self.assertEqual(path.read_bytes(),original)

    def test_local_purchase_candidate_does_not_require_legacy_focus_for_signing(self):
        catalog=self.import_file('河北仓',[(CODE,'2026-10-08','启用',100,'',None,18)])
        result=calculate({'河北仓':catalog},'河北仓',sales=[sale(),sale('@local-only')])
        local=result['rows'][1]
        self.assertEqual(local['_source'],'地采商品')
        self.assertTrue(new_products.is_new(local))
        focus.require_for(None,DAY,[result])
        self.assertEqual([(name,row['E']) for name,row in sem.signed_rows(result['rows'],None)], [('本月新品',CODE)])

    def test_same_newness_set_preserves_quantities_and_order(self):
        codes=['@b',CODE,'@a']
        entries=[(sku,'2026-11-11' if sku==CODE else '2025-11-22','启用',40,'',None,18) for sku in codes]
        catalog=self.import_file('河北仓',entries)
        legacy=write(self.root/'old.xlsx',wc.REQUIRED+['商品名称','商品标签',first_fill.FIELD],
                     list(catalog['items'].values()))
        base={'河北仓':wc.import_catalog(legacy,'河北仓')}
        sales=[sale(code,**({'日销':1,'周销':7,'月销':30} if code!=CODE else {})) for code in codes]
        old=calculate(base,'河北仓',profile=monthly([CODE]),sales=sales)
        new=calculate({'河北仓':catalog},'河北仓',sales=sales)
        project=lambda rs:[(r['E'],r['AU'],r['AS'],r.get('_new_requested'),sem.order_quantity(r),sem.order_amount(r)) for r in rs]
        self.assertEqual(project(new['rows']),project(old['rows']))
        self.assertEqual([r['E'] for r in new['rows']],codes)
