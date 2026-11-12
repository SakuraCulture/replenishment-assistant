"""Recoverable analysis orchestration. Native capture once; two independent consumers."""
import json
import uuid
from pathlib import Path
from stockout_common import atomic_json,read_json,now,today,digest,file_hash,resource_check,WAREHOUSES
from runtime_support import checkpoint,Cancelled

MODES={'store':'只跑门店','warehouse':'只跑大仓','both':'门店与大仓一起跑'}
TERMINAL={'COMPLETE','ENDED'}


class Journal:
    def __init__(self,root):
        self.root=Path(root);self.folder=self.root/'analysis'/'stockout_hub'
    def path(self,rid):
        if not isinstance(rid,str) or len(rid)!=32 or any(c not in '0123456789abcdef' for c in rid):raise ValueError('分析作业编号无效')
        return self.folder/'runs'/(rid+'.json')
    def run(self,rid):return read_json(self.path(rid))
    def change(self,rid,**values):
        row=self.run(rid);row.update(values,updated=now());atomic_json(self.path(rid),row);return row
    def recent(self):
        folder=self.folder/'runs'
        if not folder.exists():return []
        keys=('id','trigger','mode','day','state','created','updated','stores','warehouses','required','errors','store_run','warehouse_run')
        return [{k:r[k] for k in keys if k in r} for p in sorted(folder.glob('*.json'),key=lambda p:p.stat().st_mtime,reverse=True)[:100] for r in [read_json(p)]]
    def settings(self,basis):return basis


def preview(root,mode,warehouses=None):
    if mode not in MODES:raise ValueError('请选择运行范围')
    from stockout_repository import Repository as Stores
    from warehouse_stockout import Repository as Warehouses,warehouses as check_warehouses
    stores=Stores(root);wh=Warehouses(root);settings=stores.settings() if mode!='warehouse' and stores.path.exists() else None
    if mode!='warehouse' and not settings:raise ValueError('请先在门店缺货选择门店并采用供货设置')
    outputs=check_warehouses(warehouses if warehouses is not None else wh.selected()) if mode!='store' else []
    required=sorted(set(outputs)|{w for r in (settings or {}).get('routes',{}).values() for w in (r.get('primary'),r.get('secondary')) if w in WAREHOUSES})
    result={'mode':mode,'day':today(),'basis':settings,'warehouses':outputs,'required':required,
            'stores':(settings or {}).get('stores',[])}
    result['token']=digest(result)
    return result


def create(journal,plan,*,trigger='manual'):
    if preview(journal.root,plan['mode'],plan['warehouses'])!=plan:raise ValueError('选择或分析设置已变化，请重新确认运行范围')
    for old in journal.recent():
        if trigger!='manual' and old.get('trigger')==trigger:return old
        if old['state'] not in TERMINAL:
            frozen=journal.run(old['id'])
            # A finished warehouse consumer must not be held hostage by an AX
            # failure. Its native warehouse capture is already fully reconciled.
            independent=False
            if plan['mode']=='warehouse' and old['state']=='PARTIAL' and frozen['external'].get('catalogs_frozen'):
                from warehouse_stockout import Repository
                wh=Repository(journal.root)
                independent=not old.get('warehouse_run') or wh.run(old['warehouse_run'])['state']=='COMPLETE'
            if not independent:raise ValueError('请先继续或结束统一分析原作业：'+old['id'])
    rid=uuid.uuid4().hex
    row={**{k:v for k,v in plan.items() if k!='token'},'id':rid,'trigger':trigger,'state':'PREPARED','created':now(),'updated':now(),
         'external':{},'catalogs':{},'errors':{},'store_run':uuid.uuid4().hex if plan['mode']!='warehouse' else None,
         'warehouse_run':uuid.uuid4().hex if plan['mode']!='store' else None}
    atomic_json(journal.path(rid),row)
    if plan['mode']!='store':atomic_json(journal.root/'analysis'/'warehouse_stockout'/'selection.json',plan['warehouses'])
    return row


def children(journal,row):
    stores=wh=None
    if row['store_run']:
        from stockout_repository import Repository
        stores=Repository(journal.root);stores.initialize()
        stores.new_run(run_id=row['store_run'],basis_version=row['basis']['version'],trigger='hub:'+row['id'],day=row['day'],source_run=row.get('store_source'))
    if row['warehouse_run']:
        from warehouse_stockout import Repository
        wh=Repository(journal.root);wh.initialize()
        wh.new_run(row['warehouses'],run_id=row['warehouse_run'],source='hub:'+row['id'],day=row['day'])
    return stores,wh


def execute(journal,rid,rpc,cancel,progress=lambda _:None):
    from stockout_acquisition import collect_catalogs,execute as collect_stores
    from stockout_facts import load_catalog
    import warehouse_stockout as warehouse
    row=journal.run(rid)
    if row['state'] in TERMINAL:return row
    if row['day']!=today():raise ValueError('原作业已跨日；请结束原准备后采集今日，不用今日库存补历史')
    checkpoint(cancel);resource_check(journal.root);stores,wh=children(journal,row)
    errors={};journal.change(rid,state='RUNNING',errors=errors)
    try:
        def load(repo,path,code,**values):
            checkpoint(cancel)
            observed=row['day']+'T'+now().split('T',1)[1]
            if today()!=row['day']:raise ValueError('仓采集已跨日，请采集今日')
            if stores:
                load_catalog(stores,path,code,observed=observed,**values)
            if wh and code in row['warehouses']:
                if code in wh.run(row['warehouse_run'])['external'].get('deleted_warehouses',[]):raise ValueError('原大仓分析已删除，旧任务不会恢复它')
                with wh.connect() as db:
                    existing=db.execute('SELECT s.* FROM run_snapshots r JOIN snapshots s ON s.id=r.snapshot WHERE r.run=? AND r.warehouse=?',(row['warehouse_run'],code)).fetchone()
                if existing and existing['source_sha']!=values['expected']:raise ValueError('原仓快照与回执不同，未换用新库存')
                if not existing:
                    if wh.run(row['warehouse_run'])['state']=='COMPLETE':raise ValueError('原大仓结果已删除，不能由旧任务恢复')
                    warehouse.load_master(wh,path,code,run=row['warehouse_run'],observed=observed,**values)
                wh.publish(row['warehouse_run'],code,once=True)
                progress(code+'大仓结果已保存；继续其余分析')
            return values['expected']
        try:
            if not row['external'].get('catalogs_frozen'):
                collect_catalogs(journal,rid,rpc,cancel,progress,warehouses=row['required'],loader=load)
                latest=journal.run(rid);external=latest['external'];external['catalogs_frozen']=not external.get('catalogs_pending',False)
                journal.change(rid,external=external)
        except Cancelled:raise
        except Exception as exc:errors['catalog']=str(exc);progress('仓资料部分未完成：'+str(exc))
        row=journal.run(rid)
        if wh:
            with wh.connect() as db:ready=[r[0] for r in db.execute('SELECT warehouse FROM run_snapshots WHERE run=?',(row['warehouse_run'],))]
            wh.change(row['warehouse_run'],state='COMPLETE' if len(ready)==len(row['warehouses']) else 'PARTIAL',message=f'已留存 {len(ready)}/{len(row["warehouses"])} 仓；统一作业 '+rid)
            if len(ready)!=len(row['warehouses']):errors['warehouse']='部分仓资料未完成，未采仓不补0'
        checkpoint(cancel)
        if stores:
            child=stores.run(row['store_run'])
            if any(s['state']=='CLEANED' for s in child['stores']):raise ValueError('原门店分析已删除，旧作业不会恢复它')
            if child['state']=='PARTIAL':
                with stores.connect() as db:
                    own=db.execute('SELECT max(id) FROM publications WHERE run=? AND day=?',(child['id'],row['day'])).fetchone()[0]
                    current=db.execute('SELECT publication FROM current_days WHERE day=?',(row['day'],)).fetchone()
                if not own or not current or current[0]!=own:raise ValueError('门店日报已更新或删除；旧组合任务不会覆盖它，请核对当前日报')
                new=uuid.uuid4().hex;journal.change(rid,store_source=child['id'],store_run=new)
                row=journal.run(rid);stores.new_run(run_id=new,source_run=child['id'],trigger='hub:'+rid,day=row['day'])
                child=stores.run(new)
            if child['state']=='ENDED':raise ValueError('门店子任务已结束，旧统一任务不会重建它')
            if child['state']!='COMPLETE':
                required={a for r in row['basis'].get('routes',{}).values() for a in (r.get('primary'),r.get('secondary')) if a}
                ext=child['external'];ext.update(hub_owner=rid,catalogs_frozen=True,catalogs_pending=any(w not in row['catalogs'] for w in required))
                stores.change(child['id'],external=ext,catalogs={w:s for w,s in row['catalogs'].items() if w in required})
                try:
                    result=collect_stores(stores,child['id'],rpc,cancel,progress,catalogs_prepared=True)
                    if result['state']!='COMPLETE':errors['store']='门店已完成部分范围；可继续补未完成项'
                except Cancelled:raise
                except Exception as exc:errors['store']=str(exc);progress('门店分析未完成：'+str(exc))
        return journal.change(rid,state='PARTIAL' if errors else 'COMPLETE',errors=errors)
    except BaseException as exc:
        journal.change(rid,state='PAUSED' if isinstance(exc,Cancelled) else 'REVIEW',errors={**errors,'run':str(exc)})
        raise


def end(journal,rid,rpc,cancel):
    row=journal.run(rid)
    if row['state'] in TERMINAL:return row
    # Keep native task ownership, results and receipts; reconcile rather than detach.
    from stockout_acquisition import Client,finish_local
    if row['external'].get('catalog_operation'):
        state=Client(rpc,'jst').request('sync.status')
        batch=next((b for b in state.get('batches',[]) if b.get('operation_id')==row['external']['catalog_operation']),None)
        if batch and state.get('running'):
            Client(rpc,'jst').request('sync.stop',batch_id=batch['id'])
            raise ValueError('已请求仓采集安全停止，请稍后再次结束；原任务保留')
    if row['store_run']:
        from stockout_repository import Repository
        repo=Repository(journal.root)
        if repo.path.exists():
            child=repo.run(row['store_run'])
            if child['state'] not in ('COMPLETE','PARTIAL','ENDED'):finish_local(repo,child['id'],rpc,cancel)
    return journal.change(rid,state='ENDED')
