"""Frozen-old/new query equivalence and bounded read work on synthetic databases."""
import copy,hashlib,importlib.util,json,os,sqlite3,tempfile,threading,unittest
from contextlib import closing
from datetime import date,timedelta
from functools import lru_cache
from pathlib import Path
from unittest.mock import patch
import work_repository as wr
import normal_orders as normal
import storage_chunks as chunks
import lifecycle_home,daily_batch,history_read
import order_lifecycle as lifecycle
import jst_orders,jst_tasks
import result_semantics as sem
from test_archive_orders import seed as archive_seed

BASE=Path(os.environ.get('REPLENISHMENT_FROZEN_0525',r'D:\优团工作文件夹\outputs\replenishment_mvp_20261029\build_0.52.5_rc1\source'))


def seed(root,parents=8,rows_per_order=4,catalog_items=512,active_parents=None):
    """Reusable purely synthetic fixture; writes only under a new caller-owned root."""
    root=Path(root);root.mkdir(parents=True,exist_ok=False)
    if parents<2 or rows_per_order<2 or rows_per_order%2:raise ValueError('Use at least two parents and an even row count')
    work,repo,record=archive_seed(root)
    today=wr.today();past=(date.fromisoformat(today)-timedelta(days=7)).isoformat();active=max(1,parents//4) if active_parents is None else active_parents
    if not 1<=active<=parents:raise ValueError('Active parents must be within the synthetic parent range')
    with closing(work.connect()) as db,db:
        previous=chunks.load(db.execute('SELECT payload FROM normal_work_revisions WHERE revision=?',('frozen',)).fetchone()[0],db)
        for catalog in previous['catalogs'].values():
            catalog['items']={f'MASTER-{i:05d}':{'name':'合成仓资料','detail':'x'*64} for i in range(catalog_items)}
        db.execute('UPDATE normal_work_revisions SET payload=? WHERE revision=?',(normal.encode(previous,db),'frozen'))
        db.execute('DELETE FROM normal_orders');db.execute('DELETE FROM normal_revisions')
        lifecycle.schema(db);jst_orders.schema(db);jst_tasks.schema(db)
        for index in range(parents):
            code=f'QA{index:04d}';oid=f'qa-parent-{index:04d}';day=today if index<active else past;number=f'BH-{day.replace("-","")}-{code}-ZC'
            payload=copy.deepcopy(record);result=payload['result'];result.update(store=code,store_name='合成门店'+code)
            rows=[]
            for position in range(rows_per_order):
                item=copy.deepcopy(result['rows'][position%2]);item.update(A=result['store_name'],B=code,E=f'{position+1:06d}',D='合成商品'+str(position+1));rows.append(item)
            result['rows']=rows;result['summary']=sem.summarize(rows)
            digest=normal.fingerprint(result,payload.get('new_focus'));blob=normal.encode(payload,db)
            db.execute('INSERT INTO normal_orders VALUES(?,?,?,?,?,?)',(day,code,'normal_model',oid,number,1))
            db.execute('INSERT INTO normal_revisions VALUES(?,?,?,?,?)',(oid,1,digest,day+'T09:00:00+08:00',blob))
            for warehouse in ('河北仓','山东仓'):
                head={'oid':oid,'revision':1,'warehouse':warehouse,'day':day,'store':code,'number':number,'digest':digest,'fixture_kind':'task'}
                if warehouse=='河北仓' and index%3==0:
                    document={'schema':2,'files':[],'rows':[{'sku':'000001','name':'合成商品1','base_quantity':'5'}],'basis':{'dependencies':{}},'pending':False}
                    db.execute('INSERT INTO lifecycle_versions VALUES(?,?,?,?,?,?,?,?)',(oid,1,warehouse,'return',1,wr.now(),lifecycle.fingerprint(document),wr.dumps(document)))
                # Old-revision existence must remain visible after query scoping.
                old={'schema':1,'files':[],'rows':[]}
                db.execute('INSERT INTO lifecycle_versions VALUES(?,?,?,?,?,?,?,?)',(oid,0,warehouse,'return',1,wr.now(),lifecycle.fingerprint(old),wr.dumps(old)))
                gid=f'g-{index:04d}-{warehouse}'
                generated={'revision':1,'return':None,'summary':{'allocated':index+1},'fixture_kind':'active'}
                db.execute('INSERT INTO jst_headers VALUES(?,?)',(gid,wr.dumps(generated)))
                db.execute('INSERT INTO jst_active VALUES(?,?,?)',(oid,warehouse,gid))
                db.execute('INSERT INTO jst_locks VALUES(?,?,?,?,?,?)',(oid,warehouse,gid,index%2,wr.now(),'合成锁单'))
                for version in (1,2):
                    db.execute('INSERT INTO jst_tasks VALUES(?,?,?,?,?,?,?)',(gid+'-'+str(version),'2026-01-01T09:00:00+08:00',wr.dumps(head),'{}','pending' if version==1 else 'failed',None,'合成最新任务'+str(version)))
    return work,repo,{'from':today,'to':today,'search':'','all_records':True},active


@lru_cache(maxsize=1)
def frozen():
    if not (BASE/'src/history_read.py').is_file():raise unittest.SkipTest('Frozen 0.52.5 comparison source is not available')
    manifest=json.loads((BASE.parent/'source-manifest.json').read_text('utf-8'))
    modules=[]
    for name in ('history_read','lifecycle_home','daily_batch'):
        path=BASE/'src'/(name+'.py')
        if hashlib.sha256(path.read_bytes()).hexdigest()!=manifest['src/'+name+'.py']:raise AssertionError('Frozen comparison source changed: '+name)
        spec=importlib.util.spec_from_file_location('frozen_0525_'+name,path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);modules.append(module)
    modules[2].lifecycle_home=modules[1]
    return tuple(modules)


def observed(call):
    """Count real SQL statements and marked JSON decodes without changing outputs."""
    connect=sqlite3.connect;loads=wr.loads;statements=[];decodes={'task':0,'active':0}
    def opened(*args,**kwargs):
        db=connect(*args,**kwargs);db.set_trace_callback(statements.append);return db
    def decoded(raw):
        value=loads(raw)
        if isinstance(value,dict) and value.get('fixture_kind') in decodes:decodes[value['fixture_kind']]+=1
        return value
    with patch.object(sqlite3,'connect',side_effect=opened),patch.object(wr,'loads',side_effect=decoded):result=call()
    counts={'statements':len(statements),'chunk_reads':sum('SELECT size,data FROM snapshot_chunks' in q for q in statements),**decodes}
    return result,counts,statements


class ScopedQueriesTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.work,self.repo,self.query,self.active=seed(self.root/'data')
        self.path=self.work.path;self.old_history,self.old_lifecycle,self.old_daily=frozen()
    def compare(self,name,request):
        old=getattr(self,'old_'+name);new={'lifecycle':lifecycle_home,'daily':daily_batch}[name]
        first=old.read(self.path,self.root/('old-'+name+'.sqlite3'),request)
        second=new.read(self.path,self.root/('new-'+name+'.sqlite3'),request)
        self.assertEqual(first,second)
        return second
    def test_lifecycle_full_result_status_pagination_and_old_revision_equivalence(self):
        for request in [self.query,{'from':'','to':'','search':''},{'from':'','to':'','status':'全部未完成','page':1},
                        {**self.query,'search':'合成门店QA0000 河北仓'},{**self.query,'from':'1900-01-01','to':'1900-01-02'}]:
            with self.subTest(request=request):result=self.compare('lifecycle',request)
        result=self.compare('lifecycle',self.query)
        self.assertTrue(all(r['old_revision'] for r in result['records']))
        self.assertEqual(result['scope'],self.active*2)
    def test_daily_fixed_scope_stage_warehouse_lock_and_all_matched_equivalence(self):
        all_records=self.compare('lifecycle',self.query)['records'];one=all_records[0]
        for request in [self.query,{**self.query,'warehouse':'河北仓','lock':'已锁'},
                        {**self.query,'scope':[[one['oid'],one['warehouse']]],'all_matched':True},
                        {**self.query,'stage_view':'jst','stage_filter':'已生成未锁'},
                        {'from':'','to':'','page':1,'status':'全部未完成'},
                        {**self.query,'from':'1900-01-01','to':'1900-01-02'}]:
            with self.subTest(request=request):self.compare('daily',request)
    def test_daily_only_decodes_in_range_tasks_and_generations(self):
        old,previous,_=observed(lambda:self.old_daily.read(self.path,self.root/'count-old.sqlite3',self.query))
        new,current,sql=observed(lambda:daily_batch.read(self.path,self.root/'count-new.sqlite3',self.query))
        self.assertEqual(old,new)
        self.assertEqual(previous['task'],8*2*2);self.assertEqual(current['task'],self.active*2*2)
        self.assertEqual(previous['active'],8*2);self.assertEqual(current['active'],self.active*2)
        self.assertTrue(any('FROM lifecycle_versions WHERE oid IN (' in q for q in sql))
        self.assertTrue(any('FROM jst_tasks WHERE CASE WHEN json_valid(head)' in q for q in sql))
        self.assertTrue(all(r['generation_task']['message']=='合成最新任务2' for r in new['records']))
    def test_history_directory_keyword_search_and_metadata_projection_equivalence(self):
        for terms in ('','合成门店QA0000 合成商品1','000001','无匹配词'):
            request={'kind':'list','from':self.query['from'],'to':self.query['to'],'search':terms,'store':'QA0000','warehouse':'河北仓'}
            old,previous,_=observed(lambda:self.old_history.execute(self.path,self.root/('history-old-'+str(len(terms))+'.sqlite3'),request,threading.Event(),lambda _:None))
            new,current,_=observed(lambda:history_read.execute(self.path,self.root/('history-new-'+str(len(terms))+'.sqlite3'),request,threading.Event(),lambda _:None))
            self.assertEqual(old,new)
            self.assertEqual(len(new['records']),16)  # unfiltered directory contract
            if not terms:self.assertLess(current['chunk_reads'],previous['chunk_reads'])
    def test_empty_scope_does_not_scan_unrelated_versions_tasks_or_active_payloads(self):
        query={'from':'1900-01-01','to':'1900-01-02'}
        result,counts,sql=observed(lambda:daily_batch.read(self.path,self.root/'empty.sqlite3',query))
        self.assertEqual(result['records'],[]);self.assertEqual(counts['task'],0);self.assertEqual(counts['active'],0)
        for table in ('lifecycle_versions GROUP BY','SELECT a.oid,a.warehouse'):
            self.assertFalse(any(table in query for query in sql))
        self.assertEqual(sum('FROM jst_tasks WHERE CASE WHEN json_valid(head)' in q for q in sql),1)
    def test_global_header_change_outside_requested_day_still_rejects(self):
        original=normal.Repository.headers
        def run(module,name):
            count=[0]
            def changed(repo):
                value=original(repo);count[0]+=1
                if count[0]==2:value=[*value,{'oid':'other-day-changed'}]
                return value
            with patch.object(normal.Repository,'headers',changed),self.assertRaisesRegex(wr.Conflict,'签发修订在查询期间变化'):
                module.read(self.path,self.root/(name+'.sqlite3'),self.query)
        run(self.old_lifecycle,'cas-old');run(lifecycle_home,'cas-new')
    def test_more_than_sqlite_legacy_parameter_limit_and_task_order(self):
        from query_scope import oid_batches
        values=['same','same',*[str(i) for i in range(1201)]];batches=list(oid_batches(values))
        self.assertEqual([v for _,batch in batches for v in batch],list(dict.fromkeys(values)))
        self.assertTrue(all(len(batch)<=400 and placeholders.count('?')==len(batch) for placeholders,batch in batches))
    def test_real_second_page_keeps_total_counts_and_list_positions(self):
        work,repo,query,active=seed(self.root/'paged',parents=32,rows_per_order=2,catalog_items=8,active_parents=30)
        request={**query,'all_records':False,'page':1}
        old=self.old_daily.read(work.path,self.root/'paged-old.sqlite3',request)
        new=daily_batch.read(work.path,self.root/'paged-new.sqlite3',request)
        self.assertEqual(old,new);self.assertEqual(new['page'],1)
        self.assertEqual(new['scope'],60);self.assertEqual(new['count'],60);self.assertEqual(len(new['records']),10)
        self.assertEqual(sum(new['counts'].values()),60)
        self.assertEqual([r['list_position'] for r in new['records']],list(range(50,60)))
    def test_selected_metadata_chunk_corruption_is_not_silently_skipped(self):
        import zlib
        with closing(self.work.connect()) as db,db:
            blob=db.execute('SELECT payload FROM normal_work_revisions WHERE revision=?',('frozen',)).fetchone()[0]
            node=chunks.require_manifest(blob)['tree']
            for name in ('catalogs','河北仓','source_sha256'):node=next(v for k,v in node[1] if k==name)
            self.assertEqual(node[0],'ref');db.execute('UPDATE snapshot_chunks SET data=? WHERE hash=?',(b'broken-compressed-data',node[1]))
        request={'kind':'list','from':'','to':''}
        for module,name in ((self.old_history,'broken-old'),(history_read,'broken-new')):
            with self.subTest(module=name),self.assertRaises((ValueError,zlib.error)):
                module.execute(self.path,self.root/(name+'.sqlite3'),request,threading.Event(),lambda _:None)
    def test_unassignable_task_identity_is_not_silently_hidden_by_scope(self):
        with closing(self.work.connect()) as db,db:
            db.execute('INSERT INTO jst_tasks VALUES(?,?,?,?,?,?,?)',('bad-identity',wr.now(),wr.dumps({'warehouse':'河北仓'}),'{}','pending',None,'损坏合成身份'))
        for module,name in ((self.old_daily,'bad-task-old'),(daily_batch,'bad-task-new')):
            with self.subTest(module=name),self.assertRaises(KeyError):
                module.read(self.path,self.root/(name+'.sqlite3'),self.query)
    def test_malformed_and_nontext_task_identities_keep_frozen_results_and_errors(self):
        cases=[('{broken-json',json.JSONDecodeError),('[]',TypeError),('null',TypeError),
               (wr.dumps({'warehouse':'河北仓'}),KeyError),
               (wr.dumps({'oid':['bad'],'warehouse':'河北仓'}),TypeError),
               (wr.dumps({'oid':'qa-parent-0000','warehouse':['bad']}),TypeError),
               (wr.dumps({'oid':23,'warehouse':'河北仓'}),None),
               (wr.dumps({'oid':'qa-parent-0000','warehouse':None}),None),
               (wr.dumps({'oid':False,'warehouse':23}),None)]
        for raw,error in cases:
            with closing(self.work.connect()) as db,db:
                db.execute('DELETE FROM jst_tasks WHERE task=?',('edge-identity',))
                db.execute('INSERT INTO jst_tasks VALUES(?,?,?,?,?,?,?)',('edge-identity',wr.now(),raw,'{}','pending',None,'合成身份边界'))
            for request in (self.query,{'from':'1900-01-01','to':'1900-01-02'}):
                with self.subTest(raw=raw,empty=request!=self.query):
                    outcomes=[]
                    for module,name in ((self.old_daily,'edge-old'),(daily_batch,'edge-new')):
                        try:outcomes.append(('result',module.read(self.path,self.root/(name+'.sqlite3'),request)))
                        except Exception as exc:outcomes.append(('error',type(exc),str(exc)))
                    self.assertEqual(outcomes[0],outcomes[1])
                    self.assertEqual(outcomes[0][0],'error' if error else 'result')
                    if error:self.assertEqual(outcomes[0][1],error)


if __name__=='__main__':unittest.main()
