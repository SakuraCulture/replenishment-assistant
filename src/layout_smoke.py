"""Isolated native layout and real sign-entry walkthrough; no live platforms."""
import copy,json,time,traceback
from pathlib import Path
from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QDialog,QMessageBox,QCheckBox,QPushButton
from version_info import diagnostic,MODEL_VERSION


def start_smoke(app,win,args):
    out=Path(args[0]);out.mkdir(parents=True,exist_ok=True);checks=[];geometry=[];report={}
    def require(ok,text):
        if not ok:raise AssertionError(text)
        checks.append(text)
    def run():
        nonlocal report
        try:
            import engine,result_semantics as sem,normal_delivery,normal_orders,work_repository as wr
            from preissue_ui import FinalListDialog
            stores={f'QA{i}':f'隔离门店{i}' for i in range(1,9)};rows=[];win.results={}
            for s,name in stores.items():
                goods=[]
                for i in range(40):
                    d=dict(B=s,E=f'{s}-{i:03}',D='合成饮料商品'+str(i),AU='山东仓',G='瓶',BD=12,V=2,S='启用',H=1,I=7,J=30,K=0,L=0,N=0,
                        U='快销',_category='饮料',AN=2,AM=1,BH=10,BN=10,AS=12,BV=24,_source='资料匹配',_meta={'blocking':[],'notes':[]},_audit={'applied':False},
                        _preview=False,_candidate_qty=12,_candidate_amount=24,_must_sell=False)
                    goods.append(d);rows.append((d,d['_meta']))
                win.results[s]={'store':s,'store_name':name,'rows':goods,'config':{'parameters':{'cap_days':21}},'budget':{'enabled':False},'sources':{'fixture':'synthetic'},'summary':sem.summarize(goods)}
            win.loaded={'stores':stores,'normalized':rows,'blocked':{},'config':engine.config(),'warehouses':['山东仓'],'intake':{'conflicts':[]}};win.populate_stores()
            win.states={s:{'state':'已完成','name':n} for s,n in stores.items()};win.input_dirty=False
            win.current_store='QA1';win.result=win.results['QA1'];win.show_section(1);win.navigate(1)
            for width,height in [(1366,900),(1280,800)]:
                win.resize(width,height);QTest.qWait(80)
                require(win.store_table.height()>=win.store_page.height()*.4,'Store-maintenance table owns at least 40 percent of its work page')
                geometry.append({'page':'stores','window':[win.width(),win.height()],'table_height':win.store_table.height(),'page_height':win.store_page.height()})
                win.grab().save(str(out/f'stores-{width}.png'))
            require(not win.defaults_btn.isVisible(),'Low-frequency default/data-maintenance actions are folded by default')
            win.store_parameters_toggle.click();QTest.qWait(40);require(win.defaults_btn.isVisible(),'All original maintenance controls remain reachable in expanded scroll panel');win.store_parameters_toggle.click()
            before=copy.deepcopy(win.results);win.set_rows(win.result['rows'],True);win.navigate(4)
            for width,height in [(1366,900),(1280,800)]:
                win.resize(width,height);QTest.qWait(80)
                page=win.pages.widget(4)
                require(win.table.height()>=page.height()*.45,'Store goods table owns at least 45 percent of its work page')
                geometry.append({'page':'goods','window':[win.width(),win.height()],'table_height':win.table.height(),'page_height':page.height()})
                win.grab().save(str(out/f'goods-{width}.png'))
            win.result_summary_toggle.click();QTest.qWait(40);require(win.metric_values[0].isVisible(),'Full original amount and budget details remain accessible');win.result_summary_toggle.click()
            import display_settings
            display_settings.apply({'font':12,'density':'roomy'});win.resize(1366,900);QTest.qWait(80)
            require(win.table.height()>220,'Goods table remains usable with 12-point roomy display')
            win.grab().save(str(out/'goods-12pt.png'));display_settings.apply({'font':10,'density':'standard'})
            require(win.results==before,'Layout expand/collapse and navigation do not mutate results')
            win.preissue_drafts={'QA1':{'edits':{},'status':'待核对'}}
            for entry in ['sign','export']:
                phase=[];timer=QTimer(win);timer.setInterval(30)
                def control():
                    modal=app.activeModalWidget()
                    if isinstance(modal,QMessageBox):
                        phase.append(modal.text()+'\n'+modal.informativeText());modal.grab().save(str(out/(entry+'-pending.png')))
                        for b in modal.buttons():
                            if '打开这些门店' in b.text():b.click();return
                        modal.reject()
                    elif isinstance(modal,FinalListDialog):
                        phase.append('final:'+','.join(modal.codes));modal.reject()
                    elif isinstance(modal,QDialog):
                        for c in modal.findChildren(QCheckBox):c.setChecked(c.text().startswith(stores['QA1']))
                        for b in modal.findChildren(QPushButton):
                            if b.text()=='下一步：核对归档':b.click();return
                timer.timeout.connect(control);timer.start()
                try:
                    if entry=='sign':win.confirm_normal()
                    else:win.export_current(['QA1'])
                finally:timer.stop()
                require(any('隔离门店1（QA1）' in x and '应用最终数量' in x for x in phase),entry+' entry explains pending store by name/code and explicit application step')
                require('final:QA1' in phase,entry+' entry can open exactly the pending store final list without applying it')
                require('QA1' in win.preissue_drafts,entry+' cancellation retains pending edits')
            repo=normal_orders.Repository(win.work_repository);require(repo.headers()==[],'Blocked pure-sign and export entries create no signed orders')
            win.preissue_drafts={}
            # Exercise pure signing through the real selection/review/worker path.
            timer=QTimer(win);timer.setInterval(30);signed=[]
            def accept_sign():
                modal=app.activeModalWidget()
                if isinstance(modal,QDialog):
                    for c in modal.findChildren(QCheckBox):c.setChecked(c.text().startswith(stores['QA1']))
                    for b in modal.findChildren(QPushButton):
                        if b.text().startswith('确认归档 '):signed.append(True);b.click();return
                    for b in modal.findChildren(QPushButton):
                        if b.text()=='下一步：核对归档':b.click();return
            timer.timeout.connect(accept_sign);timer.start()
            # Explicit fixture snapshot: no import/admission provenance is claimed.
            payload={'schema':1,'day':win.job_day,'model_version':MODEL_VERSION,'input_dirty':False,'new_focus':None,'states':win.states,'results':win.results}
            win.snapshot=lambda:copy.deepcopy(payload)
            try:win.confirm_normal()
            finally:timer.stop();del win.snapshot
            require(bool(signed) and len(repo.headers())==1,'Actual pure-sign entry succeeds after pending edits are resolved, without exporting')
            report={'passed':True,**diagnostic(),'checks':checks,'geometry':geometry,'synthetic':True,'live_platform':False}
        except Exception:report={'passed':False,'checks':checks,'geometry':geometry,'error':traceback.format_exc()}
        finally:
            (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8');win.results={};win.preissue_drafts={};win.job_dirty=False;win._discard_close=True;win.close();QTimer.singleShot(150,lambda:app.exit(0 if report.get('passed') else 1))
    QTimer.singleShot(150,run)
