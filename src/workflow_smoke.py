"""Opt-in real-file workflow pressure probe; never uses the user's settings folder."""
import sys,json,time,traceback,pickle,zlib,hashlib,gc,threading
from pathlib import Path
from PySide6.QtCore import QTimer,Qt
from PySide6.QtWidgets import QApplication
from version_info import diagnostic
import work_repository as wr
import result_semantics as sem


def capture(loaded,results,settings,out,progress=lambda s:None,cancel=None):
    """Evidence only, outside the measured interactive and calculation phases."""
    out=Path(out);out.mkdir(exist_ok=True)
    def signature(sequence):
        h=hashlib.sha256()
        for item in sequence:
            raw=pickle.dumps(item,protocol=5);h.update(len(raw).to_bytes(8,'little'));h.update(raw)
        return h.hexdigest()
    # Record-by-record pickle retains tuple/date/None/number types and insertion
    # order. Identical adapter code has identical object sharing within a record.
    data=loaded['intake'];report={'stores':list(loaded['stores'].items()),'files':data['files'],'headers':data['headers'],'hashes':data['hashes'],'duplicates':data['duplicates'],'conflicts':data['conflicts'],'selected_signature':signature(data['selected']),'normalized_signature':signature(loaded['normalized']),'warehouse_signatures':{w:signature(rows) for w,rows in loaded.get('warehouse_rows',{}).items()}}
    (out/'intake.pklz').write_bytes(zlib.compress(pickle.dumps(report,protocol=5)))
    (out/'settings.json').write_text(wr.dumps(settings),encoding='utf-8')
    for s in loaded['stores']:
        if s in results:(out/(s+'.pklz')).write_bytes(zlib.compress(pickle.dumps(results[s],protocol=5)))
    return str(out)


def start_smoke(app,win,args):
    fixture,out=map(lambda p:Path(p).resolve(),args[:2]);out.mkdir(parents=True,exist_ok=True)
    spec=wr.loads(fixture.read_text(encoding='utf-8'));state={'step':'start','start':time.monotonic(),'phase':None,'last':time.perf_counter(),'gaps':{},'timings':{},'events':[],'views':[]};timer=QTimer(win);pulse=QTimer(win)
    gc_start={};gc_slow=[]
    def gc_trace(event,info):
        key=(threading.get_ident(),info['generation'])
        if event=='start':gc_start[key]=(time.perf_counter(),state['phase'])
        elif key in gc_start:
            started,phase=gc_start.pop(key);elapsed=time.perf_counter()-started
            if elapsed>.05:gc_slow.append({'phase':phase,'seconds':elapsed,'generation':info['generation'],'thread':key[0],'collected':info['collected']})
    gc.callbacks.append(gc_trace)
    def beat():
        now=time.perf_counter();phase=state['phase']
        if phase:state['gaps'][phase]=max(state['gaps'].get(phase,0),now-state['last'])
        state['last']=now
    pulse.timeout.connect(beat);pulse.start(25)
    def begin(name):
        app.processEvents();state['phase']=name;state['last']=time.perf_counter();state['begin']=state['last'];(out/'phase.txt').write_text(name,encoding='utf-8')
    def end():
        app.processEvents();name=state['phase'];state['timings'][name]={'seconds':time.perf_counter()-state['begin'],'max_ui_gap_seconds':state['gaps'].get(name,0)};state['phase']=None
    def finish(error=None):
        timer.stop();pulse.stop();state['phase']=None
        gc.callbacks.remove(gc_trace)
        report={'passed':error is None,'error':error,'version':diagnostic(),'timings':state['timings'],'gc_pauses':gc_slow,'events':state['events'],'views':state['views'],'stores':list((win.loaded or {}).get('stores',{})),'rows':len((win.loaded or {}).get('normalized',[])),'scope':spec['label'],'core_results':{s:r['summary'] for s,r in win.results.items()}}
        (out/'workflow_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');win.job_dirty=False;win._discard_close=True;QTimer.singleShot(0,win.close if error is None else lambda:app.exit(1))
    win.failed=lambda msg:finish(msg);sys.excepthook=lambda t,v,tb:finish(''.join(traceback.format_exception(t,v,tb)))
    original=win.task_changed
    def changed(store,status,result,error):
        state['events'].append({'store':store,'state':status,'time':time.monotonic()-state['start']});original(store,status,result,error)
    win.task_changed=changed
    def timed(name,fn):begin(name);v=fn();end();return v
    def tick():
        try:
            if time.monotonic()-state['start']>1500:raise TimeoutError(str(state['step']))
            if QApplication.activeModalWidget() is not None:return
            step=state['step']
            if step=='start':
                win.show_section(1);win.catalogs=spec['catalogs'];win.profile=spec['profile'];win.refresh_catalog_labels();win.refresh_profile_label();win.new_focus=spec.get('new_focus');win.refresh_focus();win.sales.add_paths(spec['paths']);win.concurrency.setValue(4)
                state['step']='loaded';begin('check_files');win.start_load()
            elif step=='loaded' and win.worker is None:
                end();assert len(win.loaded['normalized'])==spec['expected_rows'];assert not win.loaded['blocked'];assert len(win.store_widgets)==spec['expected_stores'];begin('store_settings')
                win.initializing=True
                for s in win.store_widgets:
                    if s in spec['store_presets']:win.store_presets[s]=spec['store_presets'][s];win.overrides[s]=spec['store_presets'][s]['config']['warehouses']
                    ui=spec['store_ui'].get(s,spec['extra_store_ui']);win.set_store_ui(s,ui);win.refresh_parameter_label(s)
                win.initializing=False;win.selection_changed();end();state['settings']={s:win.selected_settings(s) for s in win.selected_codes()};state['step']='calculated';begin('calculation');win.start_calculation()
            elif step=='calculated' and win.worker is None:
                end();assert len(win.results)==spec['expected_stores'];assert all(s['state']=='已完成' for s in win.states.values());assert not win.orders.list();assert len([x for x in state['events'] if x['state']=='计算中'])==spec['expected_stores']
                state['codes']=iter(win.loaded['stores']);state['step']='views'
            elif step=='views':
                s=next(state['codes'],None)
                if s is None:
                    state['step']='captured';(out/'phase.txt').write_text('qa_capture',encoding='ascii');win.job(capture,(win.loaded,win.results,state['settings'],str(out/'capture')),lambda _:None);return
                r=win.results[s];win.current_store=s;win.result=r
                timed(s+'/goods',lambda:win.set_rows(r['rows'],True));timed(s+'/all',lambda:win.mode.setCurrentText('全部商品'))
                assert win.proxy.rowCount()==sum(not sem.exclusion_reason(d) for d in r['rows'])
                timed(s+'/sort',lambda:win.table.sortByColumn(5,Qt.SortOrder.DescendingOrder))
                if win.proxy.rowCount():timed(s+'/detail',lambda:win.show_detail(win.proxy.index(0,0)))
                from new_product_ui import NewProductDialog
                dialog=timed(s+'/new',lambda:NewProductDialog(win,r,r,win.new_focus));dialog.show();dialog.repaint();app.processEvents();assert all(not sem.exclusion_reason(d) for d in dialog.rows)
                state['views'].append({'store':s,'raw':len(r['rows']),'visible':win.proxy.rowCount(),'new':len(dialog.rows)});dialog.close();dialog.deleteLater()
            elif step=='captured' and win.worker is None:finish()
        except Exception:finish(traceback.format_exc())
    timer.timeout.connect(tick);timer.start(80)
