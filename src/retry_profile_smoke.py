"""Isolated regression fixture: real UI scheduling, spawn and unchanged model."""
import copy,json,os,sys,time,traceback
from pathlib import Path
from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest
import engine,priority_list,work_repository as wr,result_semantics as sem

def profile(sku):return priority_list.make_profile([{'sku':sku,'name':'商品'+sku,'category':'休闲食品'}],'清单'+sku)

def loaded():
    rows=[]
    for store in ('S1','S2'):
        for sku in ('A','B'):
            d=dict(A=store,B=store,D='商品'+sku,E=sku,H=2.,I=14.,J=60.,K=0.,L=0.,N=0.,O=1.,V=1.,S='启用',AU='山东仓',U='快销',AB=.2,AC=.4,AD=.3,_source='资料匹配')
            meta=dict(export_row=len(rows)+2,product_row=len(rows)+2,match='商品编码精确匹配',blocking=[],notes=[],category='休闲食品',export_status='正常',export_updated=None,product_tags=[],product_tags_known=True,medical_sources=[])
            rows.append((d,meta))
    return dict(normalized=rows,config=engine.config(),stores={'S1':'合成门店1','S2':'合成门店2'},blocked={},hashes={},intake={'files':[],'conflicts':[],'duplicates':0,'products':''})

def wait(win):
    deadline=time.monotonic()+45
    while win.worker is not None and time.monotonic()<deadline:QTest.qWait(20)
    if win.worker is not None:raise AssertionError('计算任务未安全结束')
    if any(s['state']=='计算失败' for s in win.states.values()):raise AssertionError(str(win.states))

def install(win,sku,budget='10'):
    win.profile=profile(sku) if sku else None;win.loaded_ok(loaded());win.refresh_profile_label()
    for code in win.store_widgets:win.set_store_ui(code,{'mode':2 if budget is not None else 1,'amount':budget or '','primary':'山东仓','secondary':None,'selected':True})

def quantities(result):return {d['E']:sem.order_quantity(d) for d in result['rows']}

def exercise(win,*,owned=True,budget='10',restored_profile='B',verify=True):
    install(win,restored_profile,budget);win.start_calculation();wait(win)
    expected=copy.deepcopy(win.results['S1'])
    win.results.pop('S1');win.states['S1'].update(state='计算失败',error='合成瞬时失败')
    snapshot=copy.deepcopy(win.snapshot());old=win.work_repository.get_job(win.job_day)
    win.work_repository.save_job(win.job_day,snapshot,old['revision'] if old else None)
    saved=win.work_repository.get_job(win.job_day)
    # Compare untouched stores to the persisted tree, including codec container types.
    sibling=copy.deepcopy(saved['payload']['results']['S2'])
    install(win,'A');win.start_calculation();wait(win);task_a=quantities(win.results['S1'])
    win._queued_retries=['S2'];win.restore_job(saved,owned=owned)
    displayed=copy.deepcopy(win.profile);win.retry_stores(['S1']);wait(win)
    actual=win.results['S1'];report={'task_a':task_a,'expected':quantities(expected),'actual':quantities(actual),'displayed_profile':displayed['revision'] if displayed else None,'used_profile':(actual.get('must_list') or {}).get('revision'),'amount':actual['summary']['amount'],'sibling_preserved':win.results['S2']==sibling,'queue_cleared':not win._queued_retries}
    if verify:
        if report['actual']!=report['expected'] or report['used_profile']!=report['displayed_profile']:raise AssertionError(report)
        if not report['sibling_preserved'] or not report['queue_cleared']:raise AssertionError(report)
        # Timing/build metadata is irrelevant to the actual SKU calculation rows.
        if actual['rows']!=expected['rows'] or actual['budget']!=expected['budget']:raise AssertionError('相同B快照重跑业务字段不同')
    return report

def start_smoke(app,win,args):
    out=Path(args[0]).resolve();beats=[]
    def run():
        timer=QTimer(win);timer.setInterval(25);timer.timeout.connect(lambda:beats.append(time.monotonic()));timer.start()
        try:
            if Path(os.environ.get('REPLENISHMENT_DATA_DIR','')).resolve()!=out/'settings':raise ValueError('必须使用隔离QA目录')
            win.auto_check.stop();win.jst_generator.stopped=True;win.jst_generator.timer.stop();win.aoxiang_workflows.stopping=True;win.aoxiang_workflows.timer.stop()
            evidence=exercise(win);win.navigate(3);app.processEvents();win.grab().save(str(out/'恢复B后重跑.png'))
            from version_info import diagnostic
            result={'passed':True,'frozen_exe':bool(getattr(sys,'frozen',False)),'scenario':evidence,'heartbeat_ticks':len(beats),**diagnostic()}
        except Exception:result={'passed':False,'error':traceback.format_exc(),'frozen_exe':bool(getattr(sys,'frozen',False))}
        timer.stop();out.mkdir(parents=True,exist_ok=True);(out/'retry-report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),'utf-8');win._discard_close=True;app.exit(0 if result['passed'] else 1)
    QTimer.singleShot(200,run)
