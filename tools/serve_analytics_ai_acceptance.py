"""Isolated 0.39 analytics workspace for real model-driven client acceptance.

Synthetic fixtures are made before testing. Clients choose tools themselves;
the harness records calls and proves formal data/current work did not change.
"""
import argparse,copy,hashlib,json,multiprocessing,os,sqlite3,sys,time
from contextlib import closing
from datetime import date,timedelta
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def business_digest(path):
    with closing(sqlite3.connect(path)) as db:
        values={}
        for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
            if table in ('automation_tasks','automation_events'):continue
            rows=db.execute('SELECT * FROM "'+table.replace('"','""')+'"').fetchall()
            if table=='sqlite_sequence':rows=[r for r in rows if r[0]!='automation_events']
            values[table]=hashlib.sha256(repr(rows).encode('utf-8')).hexdigest()
    return values


def fixture(out):
    sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]
    import test_order_analytics as fixtures
    import normal_orders as no,work_repository as wr,purchases
    f=fixtures.AnalyticsTests();f.setUp()
    try:
        path=f.path;days=[(date.today()-timedelta(days=d)).isoformat() for d in (7,4,0)]
        with closing(sqlite3.connect(path)) as db,db:
            db.execute('DELETE FROM normal_orders');db.execute('DELETE FROM normal_revisions')
            for i,day in enumerate(days):
                p=copy.deepcopy(f.p);r=p['result'];r.update(store='S1',store_name='合成门店1')
                for row in r['rows']:
                    row.update(A='S1',B='合成门店1',J=30,K=0,L=0,N=0,G='包')
                if i==2:r['rows'][0].update(_budget_active=True,_allocated_qty=0,_allocated_amount=0)
                oid='ai-history-'+str(i)
                db.execute('INSERT INTO normal_orders VALUES(?,?,?,?,?,?)',(day,'S1','normal_model',oid,'BH-'+day.replace('-','')+'-S1-ZC',1))
                db.execute('INSERT INTO normal_revisions VALUES(?,?,?,?,?)',(oid,1,no.fingerprint(r),wr.now(),no.encode(p)))
        heads=purchases.targets(path)
        for h in heads:
            if h['warehouse']!='河北仓' or h['day']==days[-1]:continue
            f.f.h=h
            values=(12,6,3) if h['day']==days[0] else (24,24,24)
            for stage,quantity in zip(('return','payment','receipt'),values):
                import lifecycle_io as io
                f.f.save(stage,[{'门店编码':'S1',io.QUANTITIES[stage]:quantity,'实际金额':19.5 if stage=='payment' and quantity==6 else quantity*6.8}])
        settings=out/'settings';settings.mkdir()
        with closing(sqlite3.connect(path)) as src,closing(sqlite3.connect(settings/'workbench.sqlite3')) as dst:src.backup(dst)
    finally:f.tearDown()
    return {'from':days[0],'to':days[-1],'stores':['S1'],'rounds':8}


def main():
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);p.add_argument('--bridge-exe',type=Path,required=True);a=p.parse_args()
    out=a.out.resolve();out.mkdir(parents=True,exist_ok=False);bridge=a.bridge_exe.resolve()
    spec=fixture(out);os.environ['REPLENISHMENT_DATA_DIR']=str(out/'settings')
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QTimer
    from application_window import ApplicationWindow
    from ui_controls import STYLE
    from retry_profile_smoke import install
    import batch_service,order_analytics as analytics,export_settings,stock_diagnosis,work_repository as wr
    app=QApplication([]);app.setStyle('Fusion');app.setStyleSheet(STYLE)
    win=ApplicationWindow();win.auto_check.stop();win.jst_generator.stopped=True;win.jst_generator.timer.stop()
    win.aoxiang_workflows.stopping=True;win.aoxiang_workflows.timer.stop()
    win.setWindowTitle('AI真实客户端验收 · 合成数据 · 不连接平台');install(win,'B');win.auto_check.stop();win.concurrency.setValue(2)
    # Use the same batch entry as the UI: it freezes source audit and splits stores.
    def initial_result(code,state,result,error):
        win.states[code]['state']=state
        if error:raise RuntimeError(error)
        if result is not None:win.results[code]=result
    batch_service.run(win.loaded,{s:win.selected_settings(s) for s in ('S1','S2')},win.profile,2,initial_result)
    win.refresh_tables();win.automation.enable();win.show();win.navigate(3)
    exports=out/'exports';exports.mkdir();export_settings.save(Path(win.repository.folder)/export_settings.FILENAME,str(exports))
    def save(name,value):
        temp=out/(name+'.tmp');temp.write_bytes(json.dumps(value,ensure_ascii=False,indent=2,default=str).encode('utf-8'));os.replace(temp,out/name)
    config={'command':str(bridge),'args':['--connection',str(win.automation.connection_file)]}
    save('clients.json',{'workbuddy':{'mcpServers':{'replenishment':config}}})
    oracle=analytics.collect(win.work_repository.path,spec)
    save('oracle.json',oracle)
    before=copy.deepcopy(win.snapshot());formal=business_digest(win.work_repository.path)
    original=win.automation.dispatch
    def dispatch(action,arguments):
        record={'at':time.time(),'tool':action,'arguments':arguments}
        try:
            value=original(action,arguments);record['result']=value;return value
        except Exception as exc:record['error']=str(exc);raise
        finally:
            with (out/'tool-calls.jsonl').open('a',encoding='utf-8') as f:f.write(json.dumps(record,ensure_ascii=False,default=str)+'\n')
    win.automation.dispatch=dispatch
    last=[None]
    def state():
        return {'formal_unchanged':formal==business_digest(win.work_repository.path),'current_unchanged':before==win.snapshot(),
                'tasks':win.automation.journal.list(0,100),'context':win.automation.workbench_context({})}
    def tick():
        if (out/'stop').exists():
            if win.automation.worker:return
            save('final.json',state());win.job_dirty=False;win._discard_close=True;win.close();app.quit();return
        path=out/'request.json'
        if not path.exists():return
        try:request=json.loads(path.read_text('utf-8'))
        except ValueError:return
        if request['id']==last[0]:return
        last[0]=request['id'];case=request['case']
        ctx=win.automation.workbench_context({});scope={k:ctx[k] for k in ('instance_id','business_date','context_token')}
        common='这是补货助手具体业务测试，数据全为合成。只使用补货助手MCP开放工具；不要读文件/数据库/源码，不用终端、浏览器或其他工具。不保存、不签发、不采购、不采用参数或结果。先核对当前工作台与下列上下文完全一致，不一致或异常就停止受影响步骤。查询和试算新请求用新operation_key；已受理不等于完成，任务通过task_status与after游标低频等待；分页按next_offset读完，limit不超过100。\n'+json.dumps(scope,ensure_ascii=False)+'\n'
        tasks={
            'funnel':f'请分析{spec["from"]}到{spec["to"]}门店编码S1的五段补货漏斗，必采/非必采分开，用签发价看各段货值、相邻环节减少与增加。明确区分已知值、未确认、待核，实际付款金额另列。然后导出完整范围到软件默认导出目录，告诉我生成的文件和完整性。',
            'history':f'请复盘{spec["from"]}到{spec["to"]}门店S1的多轮记录，重点看商品0001：连续缺货首先应查执行履约还是参数？哪些是事实、哪些只能作为疑点、缺哪些证据？也核对0002的资料是否足够。不要算期间缺货率，不调参。',
            'diagnosis':'请对当前工作台门店S1、S2的快销分类做安全库存与资金诊断，概率上下各2.5个百分点。先解释实际参数与来源，核对其他参数固定，再完整比较两个候选的最终执行货值、必采资金、箱规覆盖、缺货风险和逐商品变化。别把理论模型减少当成实际节省；不采用任何候选，等我确认。',
            'checkpoint':'只核对当前上下文，并告诉我没有修改任何业务。'}
        if case not in tasks:save('response.json',{'id':request['id'],'error':'Unknown test case'});return
        save('response.json',{'id':request['id'],'case':case,'prompt':common+tasks[case], 'values':{'scope':scope,'query':spec},'state_before':state()})
        save('checkpoint.json',state())
    timer=QTimer(win);timer.setInterval(200);timer.timeout.connect(tick);timer.start()
    from version_info import VERSION
    save('ready.json',{'synthetic':True,'pid':os.getpid(),'backend':VERSION+' source workspace','bridge':'actual supplied packaged EXE','bridge_sha256':hashlib.sha256(bridge.read_bytes()).hexdigest(),'query':spec,'context':win.automation.workbench_context({})})
    app.exec()


if __name__=='__main__':multiprocessing.freeze_support();main()
