"""Explicitly authorized live acquisition in a new isolated QA data root. No signing."""
import argparse,hashlib,json,os,shutil,sqlite3,sys,threading,time,traceback
from pathlib import Path
from types import SimpleNamespace as NS
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))

def main():
    p=argparse.ArgumentParser();p.add_argument('--plan',required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--allow-export',action='store_true');p.add_argument('--resume',action='store_true');a=p.parse_args()
    if not a.allow_export:raise ValueError('Live native exports require explicit --allow-export')
    out=a.output.resolve();settings=out/'user';user=Path(os.environ['LOCALAPPDATA'])/'YoutuanTools/补货助手/settings'
    def backup(source,target):
        target.parent.mkdir(parents=True,exist_ok=True)
        with sqlite3.connect(source.as_uri()+'?mode=ro',uri=True) as src,sqlite3.connect(target) as dest:src.backup(dest)
    if not a.resume:
        out.mkdir(parents=True,exist_ok=False);settings.mkdir()
        import work_repository as wr
        work=wr.Repository(settings)
        with sqlite3.connect((user/'workbench.sqlite3').as_uri()+'?mode=ro',uri=True) as src,sqlite3.connect(work.path) as dest:
            src.execute('BEGIN')
            for name in ('stores','presets','replenishment_plans','global_category_mapping','store_admission'):
                schema=src.execute('SELECT sql FROM sqlite_master WHERE type=\'table\' AND name=?',(name,)).fetchone()
                if not schema:continue
                dest.execute('DROP TABLE IF EXISTS '+name);dest.execute(schema[0]);rows=src.execute('SELECT * FROM '+name).fetchall()
                if rows:dest.executemany('INSERT INTO '+name+' VALUES('+','.join('?' for _ in rows[0])+')',rows)
        for n in ('must_sell.json','store_budgets.json','store_supply.json','new_product_focus.json'):
            if (user/n).exists():shutil.copy2(user/n,settings/n)
        (settings/'warehouse_catalogs').mkdir()
        for path in (user/'warehouse_catalogs').glob('*.json'):shutil.copy2(path,settings/'warehouse_catalogs'/path.name)
        ax=user/'aoxiang/component-state';target=settings/'aoxiang/component-state';target.mkdir(parents=True)
        shutil.copytree(ax/'config',target/'config');shutil.copy2(ax/'state.guard.json',target/'state.guard.json');backup(ax/'state.sqlite',target/'state.sqlite')
        binding=target/'config/machine.binding.json';data=json.loads(binding.read_text('utf-8'));data['outputRoot']=str(out/'native-downloads');binding.write_text(json.dumps(data,ensure_ascii=False,indent=2),'utf-8')
        for n in ('jobs.sqlite3','workflows.sqlite3'):
            if (user/'aoxiang'/n).is_file():backup(user/'aoxiang'/n,settings/'aoxiang'/n)
        if (user/'aoxiang/jobs.sqlite3.guard').exists():shutil.copy2(user/'aoxiang/jobs.sqlite3.guard',settings/'aoxiang/jobs.sqlite3.guard')
        target=settings/'jst_catalog_sync';target.mkdir()
        for n in ('jobs.json','store.guard.json'):
            shutil.copy2(user/'jst_catalog_sync'/n,target/n)
        (out/'authorization.json').write_text(json.dumps({'plan':a.plan,'operation':'acquisition and calculation only','isolated':str(settings),'source':'copied persisted settings, no daily drafts/orders copied'},ensure_ascii=False,indent=2),'utf-8')
    os.environ['REPLENISHMENT_DATA_DIR']=str(settings)
    from aoxiang_session import SessionOwner,SessionGate
    from aoxiang_client import ProcessClient
    from catalog_sync_session import SessionOwner as CatalogOwner
    from scheduled_platforms import Platforms
    from scheduled_execution import freeze,check_signature,Runner
    import scheduled_replenishment as sr,replenishment_plans,work_repository as wr
    path=wr.Repository(settings).path;repo=sr.Repository(path)
    ax=NS(gate=SessionGate(),store=NS(folder=settings/'aoxiang'))
    ax.session=SessionOwner(lambda:ProcessClient(ROOT/'components/aoxiang',settings/'aoxiang/component-state'))
    owner=CatalogOwner(settings/'jst_catalog_sync');cancel=threading.Event();backend=Platforms(settings,path,ax,owner,cancel)
    report={'passed':False,'plan':a.plan,'live':True,'writes':'only isolated host settings/results and native read-only exports'}
    def progress(m):print(str(m),flush=True)
    try:
        if a.resume and (out/'run-id.txt').exists():
            rid=(out/'run-id.txt').read_text('utf-8').strip();repo.recover();r=repo.get(rid)
            if r['state'] in ('REVIEW','PAUSED'):repo.resume(rid,r['revision'])
            elif r['state']=='COMPLETE':report.update(passed=True,run=r);return
        else:
            plan=next(p for p in replenishment_plans.Repository(path).list() if p['name']==a.plan)
            s={'name':a.plan+'真实联调','plan_id':plan['id'],'stores':plan['stores'],'time':'06:30','kind':'weekly','weekdays':list(range(7)),'workers':2,'enabled':False}
            basis=freeze(settings,s,sr.stamp().date().isoformat());progress('核验预案 '+a.plan+'：'+str(len(plan['stores']))+'家门店')
            # User authorized this exact plan. Verify missing directory entries through the
            # existing read-only setup flow, saving only the isolated QA binding.
            client=backend.ax_client();configuration=client.request('configuration')
            verified={x['buyerWarehouseCode'] for x in configuration.get('targets',[])}
            if any(i['code'] not in verified for i in plan['stores']):
                binding=json.loads((settings/'aoxiang/component-state/config/machine.binding.json').read_text('utf-8'))
                check=client.request('setup.start',candidate={'edge_executable':binding['edgeExecutable'],'actual_profile_path':binding['actualProfilePath']},output_root=str(out/'native-downloads'),targets=plan['stores'])
                while check['state']=='RUNNING' or check.get('cleanup_pending'):
                    progress(check['stage']);time.sleep(.5);check=client.request('setup.status',setup_id=check['id'])
                if check['state']!='READY' or any(i['state']!='VERIFIED' for i in check['targets']):raise ValueError(str(check.get('error') or check['targets']))
                client.request('setup.commit',setup_id=check['id']);progress('预案6家门店已核验，仅保存到隔离连接配置')
            backend.preflight(basis,progress);s['check_signature']=check_signature(basis)
            rid=repo.manual(s)['id'];(out/'run-id.txt').write_text(rid,'utf-8')
        started=time.perf_counter();Runner(repo,backend,settings,cancel,progress).execute(rid)
        r=repo.get(rid);report.update(passed=r['state']=='COMPLETE',run=r,elapsed_seconds=time.perf_counter()-started)
    except Exception:report['error']=traceback.format_exc();progress(report['error'])
    finally:
        backend.release(progress)
        while not ax.session.close(timeout=.5):time.sleep(.5)
        while not owner.close(timeout=.5):time.sleep(.5)
        (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
    if not report['passed']:raise SystemExit(1)
if __name__=='__main__':
    import multiprocessing
    multiprocessing.freeze_support();main()
