"""Read-only, revision-pinned analysis over the native stockout query service."""
from contextlib import contextmanager
from datetime import date
import hashlib
import json
import time
from pathlib import Path
import stockout_metrics as metrics
from stockout_repository import Repository
from stockout_common import VERSION, TAG_VERSION, digest, now
from runtime_support import checkpoint

LIFETIME = 3600
RULES = {
    'base': '所选视角月销大于0的门店商品；默认剔除门店停采/禁采、实际供货仓禁用',
    'zero': '有销基数中门店可用库存小于等于0的商品；负库存原值保留',
    'rate': '缺货数合计/有销基数合计，不平均门店百分比；基数0或指标待核不出比例',
    'time': '每日观测时点，未采不补0；重复观察不代表全天缺货时长',
    'cause': '缺货率本身不能证明参数、仓供货或到货原因；履约无明确关联时保持未知',
}


def options(value):
    value = dict(value or {})
    if not value.get('start') or not value.get('end'):
        raise ValueError('请指定明确的起止日期，不自动补采或扩展日期')
    value.setdefault('exclude_stopped', True)
    value.setdefault('exclude_disabled', True)
    return metrics.validate(value)


def context(folder, offset=0, limit=100):
    repo = Repository(folder)
    if not repo.path.exists():
        return {'available': False, 'message': '尚无门店缺货率分析资料；未创建分析库，也未采集。', 'rules': RULES}
    with repo.connect() as db:
        total = db.execute('SELECT count(*) FROM current_days').fetchone()[0]
        days = [dict(r) for r in db.execute('SELECT day,publication AS revision FROM current_days ORDER BY day DESC LIMIT ? OFFSET ?', (min(100,limit),max(0,offset)))]
    return {'available': True, 'days': days, 'total_days': total, 'next_offset': offset+len(days) if offset+len(days)<total else None,
            'rules': RULES, 'dimensions': {k:v[1] for k,v in metrics.DIMENSIONS.items()}, 'default_exclusions': ['门店停采/禁采','实际供货仓禁用']}


def manifest(db, publications):
    ids = [p['id'] for p in publications]
    if not ids:return digest([])
    placeholders = ','.join('?' for _ in ids)
    actual = [dict(r) for r in db.execute(f'SELECT id,day,run,created FROM main.publications WHERE id IN ({placeholders}) ORDER BY day,id',ids)]
    if actual != publications:raise ValueError('原日报修订已删除或变化，请重新查询；未自动改用新日报')
    checksum = hashlib.sha256()
    for row in db.execute(f'''SELECT p.id,p.day,r.store,r.name,r.snapshot,r.state,r.error,
        s.day,s.observed,s.source_sha,s.basis,s.catalog_set,s.rows,s.state
        FROM main.publications p JOIN run_stores r ON r.run=p.run
        LEFT JOIN store_snapshots s ON s.id=r.snapshot
        WHERE p.id IN ({placeholders}) ORDER BY p.day,p.id,r.store''',ids):
        checksum.update(json.dumps(list(row),ensure_ascii=False,separators=(',',':')).encode('utf-8'));checksum.update(b'\n')
    return checksum.hexdigest()


class PinnedRepository:
    def __init__(self,folder,basis):
        self.repo=Repository(folder);self.path=self.repo.path;self.folder=self.repo.folder;self.root=self.repo.root;self.basis=basis
    @contextmanager
    def connect(self,*,write=False):
        if write:raise ValueError('AI分析只读，不能写入分析库')
        if time.time()>self.basis['expires_at']:raise ValueError('分析会话已过期，请重新查询')
        if not self.path.exists():raise ValueError('原分析资料已删除，请重新查询')
        with self.repo.connect() as db:
            ids=[p['id'] for p in self.basis['publications']]
            if any(type(n) is not int or n<1 for n in ids) or len(ids)>366:raise ValueError('固定日报清单无效')
            # URI mode=ro still prevents all main-database writes. Only this
            # connection's TEMP view shadows the moving current_days pointer.
            db.execute('PRAGMA query_only=OFF')
            db.execute('CREATE TEMP VIEW current_days AS SELECT day,id AS publication FROM main.publications WHERE id IN ('+(','.join(map(str,ids)) or 'NULL')+')')
            db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
            if manifest(db,self.basis['publications'])!=self.basis['manifest']:
                raise ValueError('原分析快照或采集覆盖已变化，请重新查询；未混入新结果')
            yield db


def freeze(folder,query):
    query=options(query);repo=Repository(folder)
    if not repo.path.exists():raise ValueError('尚无门店缺货率分析资料，请先在独立板块采集')
    with repo.connect() as db:
        db.execute('BEGIN')
        publications=[dict(r) for r in db.execute('SELECT p.id,p.day,p.run,p.created FROM current_days d JOIN publications p ON p.id=d.publication WHERE d.day BETWEEN ? AND ? ORDER BY p.day,p.id',(query['start'],query['end']))]
        basis={'publications':publications,'manifest':manifest(db,publications),'query':query,'rule_version':VERSION,'tag_version':TAG_VERSION,'created':now(),'expires_at':time.time()+LIFETIME}
    basis['snapshot_id']=digest({k:v for k,v in basis.items() if k not in ('created','expires_at')})
    return basis


def analyze(folder,query,dimensions=('store',),progress=lambda _:None,cancel=None):
    dims=metrics.dimension_keys(dimensions);basis=freeze(folder,query);repo=PinnedRepository(folder,basis)
    progress('读取固定日报的缺货数、基数与覆盖…');checkpoint(cancel)
    overview=metrics.query(repo,basis['query'],cancel=cancel)
    progress('比较观测日期，未采日期不补0…');trend=metrics.trend(repo,basis['query'],cancel=cancel)
    comparison=compare(repo,basis['query'],[r['day'] for r in trend],cancel)
    first=metrics.cuts(repo,basis['query'],dims,limit=100,cancel=cancel)
    with repo.connect():pass
    return {'basis':basis,'snapshot_id':basis['snapshot_id'],'overview':overview,'trend':trend,'comparison':comparison,
            'dimensions':list(dims),'first_cut':first,'rules':RULES}


def compare(repo,query,days,cancel=None):
    if len(days)<2:return {'available':False,'reason':'不足两个有记录日期，不计算变化，也不补0'}
    before,after=days[-2:];condition,args=metrics.where(query);ex=metrics.excluded_sql(query)
    statement=f'''WITH observations AS (
        SELECT s.day,s.store,f.sku,f.eligible,f.zero,f.bad,({ex}) AS excluded
        FROM {metrics.LINKS} JOIN facts f ON f.snapshot=s.id WHERE {condition} AND s.day IN (?,?)
      ), pairs AS (
        SELECT store,sku,
          max(CASE WHEN day=? THEN 1 ELSE 0 END) AS seen_before,
          max(CASE WHEN day=? THEN 1 ELSE 0 END) AS seen_after,
          max(CASE WHEN day=? THEN eligible=1 AND bad=0 AND NOT excluded ELSE 0 END) AS valid_before,
          max(CASE WHEN day=? THEN eligible=1 AND bad=0 AND NOT excluded ELSE 0 END) AS valid_after,
          max(CASE WHEN day=? THEN zero ELSE 0 END) AS zero_before,
          max(CASE WHEN day=? THEN zero ELSE 0 END) AS zero_after
        FROM observations GROUP BY store,sku
      ) SELECT count(*) AS observed_union,
        coalesce(sum(valid_before AND valid_after),0) AS comparable_base,
        coalesce(sum(valid_before AND valid_after AND zero_before),0) AS before_zero,
        coalesce(sum(valid_before AND valid_after AND zero_after),0) AS after_zero,
        coalesce(sum(valid_before AND valid_after AND zero_before AND NOT zero_after),0) AS observed_recovered,
        coalesce(sum(valid_before AND valid_after AND NOT zero_before AND zero_after),0) AS observed_new_zero,
        coalesce(sum(valid_before AND NOT valid_after),0) AS left_comparable_scope,
        coalesce(sum(valid_after AND NOT valid_before),0) AS entered_comparable_scope,
        coalesce(sum(NOT seen_before OR NOT seen_after),0) AS missing_one_observation FROM pairs'''
    with repo.connect() as db:
        if cancel:db.set_progress_handler(lambda:int(cancel.is_set()),1000)
        result=dict(db.execute(statement,[*args,before,after,before,after,before,after,before,after]).fetchone())
    base=result['comparable_base']
    result.update(available=True,before=before,after=after,interval_days=(date.fromisoformat(after)-date.fromisoformat(before)).days,
                  before_rate=result['before_zero']/base if base else None,after_rate=result['after_zero']/base if base else None,
                  note='仅比较两天均有效且纳入基数的同店同商品；移出、停采或缺观测不作为恢复，时点变化不代表全天状况。')
    return result


def repeated(repo,query,offset=0,limit=100,cancel=None):
    condition,args=metrics.where(query);ex=metrics.excluded_sql(query)
    sql=f'''WITH observations AS (
      SELECT s.day,s.store,s.name AS store_name,f.sku,f.name AS sku_name,
        (f.eligible=1 AND f.bad=0 AND NOT ({ex})) AS valid,
        (f.zero=1 AND f.bad=0 AND NOT ({ex})) AS is_zero
      FROM {metrics.LINKS} JOIN facts f ON f.snapshot=s.id WHERE {condition}
    ), previous AS (
      SELECT *,lag(day) OVER (PARTITION BY store,sku ORDER BY day) AS previous_day,
        lag(is_zero) OVER (PARTITION BY store,sku ORDER BY day) AS previous_zero FROM observations
    ), islands AS (
      SELECT *,sum(CASE WHEN is_zero AND previous_zero AND date(previous_day,'+1 day')=day THEN 0 ELSE 1 END)
        OVER (PARTITION BY store,sku ORDER BY day) AS streak_group FROM previous
    ), streaks AS (
      SELECT store,sku,streak_group,sum(is_zero) AS length FROM islands GROUP BY store,sku,streak_group
    ), longest AS (SELECT store,sku,max(length) AS consecutive_days FROM streaks GROUP BY store,sku), totals AS (
      SELECT store,max(store_name) AS store_name,sku,max(sku_name) AS sku_name,sum(valid) AS valid_observations,
        sum(is_zero) AS zero_observations,min(day) AS first_day,max(day) AS last_day
      FROM observations GROUP BY store,sku HAVING sum(is_zero)>0
    ) SELECT totals.*,longest.consecutive_days FROM totals JOIN longest USING(store,sku)'''
    with repo.connect() as db:
        if cancel:db.set_progress_handler(lambda:int(cancel.is_set()),1000)
        count=db.execute('SELECT count(*) FROM ('+sql+')',args).fetchone()[0]
        rows=[dict(r) for r in db.execute(sql+' ORDER BY zero_observations DESC,consecutive_days DESC,store,sku LIMIT ? OFFSET ?',[*args,limit,offset])]
    return {'items':rows,'total':count,'offset':offset,'next_offset':offset+len(rows) if offset+len(rows)<count else None,
            'note':'连续日要求自然日连续且每天有效缺货；缺采、无有销依据或待核会中断，不推断全天缺货。'}


def read(folder,report,level,dimensions=None,offset=0,limit=100,store='',sku='',cancel=None):
    repo=PinnedRepository(folder,report['basis']);query=report['basis']['query'];limit=min(100,max(1,limit))
    if level=='cuts':return metrics.cuts(repo,query,dimensions or report['dimensions'],offset=offset,limit=limit,cancel=cancel)
    if level in ('detail','zero','excluded'):
        return metrics.detail(repo,query,offset=offset,limit=limit,only_zero=level=='zero',excluded=level=='excluded',cancel=cancel)
    if level=='repeated':return repeated(repo,query,offset,limit,cancel)
    if level=='evidence':
        if not store or not sku:raise ValueError('追溯须指定准确门店编码与商品编码')
        filters={**query['filters']}
        for key,value in (('store',store),('sku',sku)):
            if filters.get(key) and value not in filters[key]:raise ValueError('证据对象不在本次分析范围')
            filters[key]=[value]
        condition,args=metrics.where({**query,'filters':filters});ex=metrics.excluded_sql(query)
        with repo.connect() as db:
            if cancel:db.set_progress_handler(lambda:int(cancel.is_set()),1000)
            source=f'FROM {metrics.LINKS} JOIN facts f ON f.snapshot=s.id WHERE {condition}'
            count=db.execute('SELECT count(*) '+source,args).fetchone()[0]
            items=[metrics.canonical_record(dict(r)) for r in db.execute(
                f'SELECT s.day,s.store,s.name AS store_name,s.observed,s.source_sha,s.basis,s.catalog_set,p.id AS revision,f.*,({ex}) AS excluded '+source+
                ' ORDER BY s.day,s.store,f.sku LIMIT ? OFFSET ?',[*args,limit,offset])]
        result={'items':items,'total':count,'offset':offset,'next_offset':offset+len(items) if offset+len(items)<count else None,
                'note':'含该精确商品被剔除的观测；excluded=1不计入本次缺货率。原始值与统计值分开保留。'}
        result['fulfillment']='尚未指定并核对原补货单，不能据缺货率自动认定少采、少发或迟到。可用订单跟踪或五段漏斗按原单查证。'
        return result
    raise ValueError('未知分析结果层次')


def export(folder,report,destination,combinations=None,include_detail=False,progress=lambda _:None,cancel=None):
    from stockout_exports import export as write
    repo=PinnedRepository(folder,report['basis'])
    path=write(repo,report['basis']['query'],destination,combinations=combinations,include_detail=include_detail,
               dimensions=report['dimensions'],progress=progress,cancel=cancel)
    return {'path':path,'snapshot_id':report['snapshot_id'],'query':report['basis']['query'],'publications':report['basis']['publications']}
