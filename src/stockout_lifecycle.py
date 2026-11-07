"""Analysis-only reruns, original-based recalculation and task-log removal."""
import json
import uuid
from pathlib import Path
from stockout_common import digest,encoded,now,today
from runtime_support import checkpoint,Cancelled

TERMINAL={'COMPLETE','PARTIAL','ENDED'}


def idle(db):
    if db.execute("SELECT 1 FROM runs WHERE state NOT IN ('COMPLETE','PARTIAL','ENDED') LIMIT 1").fetchone():
        raise ValueError('还有未完成的分析任务，请先继续或结束原任务')


def original(repo,sha,path):
    target=Path(path).resolve()
    if not target.is_relative_to((repo.folder/'originals').resolve()) or target.stem!=sha:
        raise ValueError('原件位置不属于本分析板块，未采用')
    if not target.is_file():raise ValueError('分析留存原件已缺失，不能重算；请恢复分析备份或重新采集今日资料')
    return str(target)


def preview(repo,rid,action,mode='current'):
    if action not in {'rerun','recalculate','delete_log'}:raise ValueError('未知作业操作')
    run=repo.run(rid)
    if run['state'] not in TERMINAL:raise ValueError('请先继续或结束原任务，再执行此操作')
    if action=='delete_log' and run['external'].get('log_deleted_at'):raise ValueError('该作业日志已经删除，请刷新记录')
    plan={'action':action,'run':rid,'updated':run['updated'],'state':run['state'],
          'stores':[{'code':s['store'],'name':s['name']} for s in run['stores']],
          'source_digest':digest(run)}
    with repo.connect() as db:
        idle(db)
        if action=='delete_log':
            if run['state']=='PARTIAL' and (run['external'].get('catalog_operation') or run['external'].get('ax_commands')):
                raise ValueError('这条作业仍需核对平台任务，请先点“结束本地准备”核实原任务已结束，再删除日志')
            plan['snapshots']=sum(s['snapshot'] is not None for s in run['stores'])
            plan['scope']='删除该作业的运行消息和逐店错误，并从作业列表移除；保留分析结果、原件及版本/平台任务必要关联，不删除应用总日志或平台任务'
        elif action=='rerun':
            latest=db.execute('SELECT external FROM runs ORDER BY rowid DESC LIMIT 1').fetchone()
            if latest and json.loads(latest[0]).get('catalogs_pending'):
                raise ValueError('原仓导出尚未核对完成，请先继续原任务，不重复导出')
            outside=db.execute('''SELECT r.name,r.store FROM current_days d JOIN publications p ON p.id=d.publication
                JOIN run_stores r ON r.run=p.run WHERE d.day=? AND r.store NOT IN
                (SELECT store FROM run_stores WHERE run=?) LIMIT 1''',(today(),rid)).fetchone()
            if outside:raise ValueError('今日日报还包含 '+outside['name']+'（'+outside['store']+'）等其他门店；请选择覆盖完整门店的作业重采，避免替换未选结果')
            plan.update(day=today(),basis=run['basis'],scope='按原作业门店和设置重新采集今日仓资料及门店补货建议；沿用并行导出和平台防重检查，不把今日库存写回历史日期')
        else:
            if mode not in {'current','original'}:raise ValueError('请选择当前分析设置或原作业设置')
            basis=repo.settings() if mode=='current' else repo.settings(run['basis'])
            if not basis:raise ValueError('尚未采用分析设置')
            codes={s['store'] for s in run['stores']}
            if mode=='current' and not codes.issubset({s['code'] for s in basis['stores']}):
                raise ValueError('当前分析设置未包含原作业全部门店；请先重新采用这些门店的设置，或选择按原设置重算')
            if any(s['state']=='CLEANED' for s in run['stores']):raise ValueError('该作业已有分析数据被删除，不能重算；可重新采集今日资料')
            snapshots=[dict(r) for r in db.execute('''SELECT s.*,o.path FROM run_stores r JOIN store_snapshots s ON s.id=r.snapshot
                JOIN originals o ON o.sha=s.source_sha WHERE r.run=? AND s.state='READY' ORDER BY s.store''',(rid,))]
            if not snapshots:raise ValueError('本作业没有可重算的完整快照，请先完成采集')
            publications={}
            catalog_files={}
            for item in snapshots:
                current=db.execute('SELECT d.publication,p.run FROM current_days d JOIN publications p ON p.id=d.publication WHERE d.day=?',(item['day'],)).fetchone()
                if not current or current['run']!=rid:
                    raise ValueError('该作业已不是此日期的当前日报，请选择当前已发布作业重算，避免覆盖后来采集的门店结果')
                publications[item['day']]=current['publication']
                item['path']=original(repo,item['source_sha'],item['path'])
                catalogs=json.loads(item['catalog_set'])
                route=basis.get('routes',{}).get(item['store'],{})
                required={route.get('primary'),route.get('secondary')}-{None,''}
                if mode=='current' and (not route or not required.issubset(catalogs)):
                    raise ValueError(item['name']+'（'+item['store']+'）缺少当前供货路线所需的历史仓原件；不能用今天仓资料补旧日，请选择原设置或重新采集今日资料')
                for warehouse,sha in catalogs.items():
                    key=warehouse+':'+sha
                    if key in catalog_files:continue
                    row=db.execute('SELECT c.*,o.path FROM catalogs c JOIN originals o ON o.sha=c.sha WHERE c.sha=? AND c.warehouse=?',(sha,warehouse)).fetchone()
                    if not row:raise ValueError('历史仓资料依据不完整，请恢复分析备份')
                    catalog_files[key]={**dict(row),'path':original(repo,sha,row['path'])}
            plan.update(mode=mode,basis=basis['version'],snapshots=snapshots,catalog_files=list(catalog_files.values()),
                        publications=publications,missing=len(run['stores'])-len(snapshots),
                        scope='仅用本作业留存的门店和仓原件重算，保留原观测日期；全部完成后发布新修订，旧修订保留；不会连接平台或改补货资料')
    plan['token']=digest(plan)
    return plan


def delete_log(repo,plan,cancel=None):
    checkpoint(cancel)
    with repo.connect(write=True) as db:
        db.execute('BEGIN IMMEDIATE')
        if preview(repo,plan['run'],'delete_log')!=plan:raise ValueError('作业记录已变化，请重新预览')
        run=repo.run(plan['run']);external=run['external']
        keep={k:external[k] for k in ('versions','identity','ax_commands','catalog_operation','catalog_owner',
                                     'catalog_items','catalogs_pending','catalogs_frozen') if k in external}
        if external.get('recalculate'):keep['recalculated_from']=run['source_run']
        keep['log_deleted_at']=now()
        checkpoint(cancel)
        db.execute('UPDATE runs SET message=?,external=?,updated=? WHERE id=?',
                   ('作业日志已删除；分析结果及必要关联保留',encoded(keep),now(),plan['run']))
        db.execute("UPDATE run_stores SET error='' WHERE run=?",(plan['run'],))
    return {'message':plan['scope']+'。'}


def rerun(repo,plan,rpc,cancel=None,progress=lambda _:None):
    checkpoint(cancel)
    if preview(repo,plan['run'],'rerun')!=plan:raise ValueError('作业或日期已变化，请重新预览重新采集')
    from stockout_acquisition import execute
    run=repo.new_run(trigger='rerun',day=plan['day'],fresh_source=plan['run'])
    return execute(repo,run['id'],rpc,cancel,progress)


def recalculate(repo,plan=None,*,resume=None,cancel=None,progress=lambda _:None):
    from stockout_facts import load_catalog,load_store
    from version_info import INFO
    checkpoint(cancel)
    if resume:
        run=repo.run(resume)
        if run['state'] in TERMINAL:raise ValueError('重算作业已经结束或发布，请刷新记录')
        plan=run['external'].get('recalculate')
        if not plan:raise ValueError('所选作业不是本地重算任务')
    else:
        if plan is None or preview(repo,plan['run'],'recalculate',plan['mode'])!=plan:
            raise ValueError('原件、作业或设置已经变化，请重新预览重算')
        # The derived immutable settings freeze exactly the source run's stores.
        # They never move settings_current or write back to the replenishment module.
        basis=repo.settings(plan['basis']);basis['stores']=plan['stores']
        codes={s['code'] for s in plan['stores']}
        for key in ('routes','route_issues'):
            if key in basis:basis[key]={code:value for code,value in basis[key].items() if code in codes}
        frozen=repo.save_settings(basis,make_current=False)
        rid=uuid.uuid4().hex
        with repo.connect(write=True) as db:
            db.execute('BEGIN IMMEDIATE');idle(db)
            if preview(repo,plan['run'],'recalculate',plan['mode'])!=plan:raise ValueError('重算依据已变化，请重新预览')
            external={'versions':{k:INFO[k] for k in ('version','model_version','routing_version')},'recalculate':plan}
            db.execute('INSERT INTO runs(id,day,basis,trigger,state,message,created,updated,source_run,external) VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (rid,min(plan['publications']),frozen['version'],'recalculate','PREPARED','已保存本地重算意图',now(),now(),plan['run'],encoded(external)))
            for store in plan['stores']:
                db.execute('INSERT INTO run_stores(run,store,name,state,error) VALUES(?,?,?,?,?)',
                           (rid,store['code'],store['name'],'FAILED','原作业未留存完整快照，未编造或补采数据'))
        run=repo.run(rid)
    rid=run['id']
    repo.change(rid,state='RUNNING',message='正在用留存原件重算；旧日报仍可查询')
    try:
        for item in plan['catalog_files']:
            checkpoint(cancel)
            load_catalog(repo,original(repo,item['sha'],item['path']),item['warehouse'],expected=item['sha'],
                         expected_rows=item['rows'],observed=item['observed'],cancel=cancel,progress=progress)
        for item in plan['snapshots']:
            checkpoint(cancel)
            progress('重算 '+item['name']+'（'+item['store']+'）；观测日期 '+item['day'])
            repo.change(rid,catalogs=json.loads(item['catalog_set']))
            load_store(repo,rid,item['store'],original(repo,item['source_sha'],item['path']),observed=item['observed'],
                       expected=item['source_sha'],expected_rows=item['rows'],cancel=cancel,progress=progress)
        checkpoint(cancel)
        progress('正在提交重算日报；提交完成后才可退出')
        return repo.publish(rid)
    except Cancelled as exc:
        repo.change(rid,state='PAUSED',message=str(exc));raise
    except Exception as exc:
        repo.change(rid,state='REVIEW',message=str(exc));raise
