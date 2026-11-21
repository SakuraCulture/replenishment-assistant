"""Actual source/frozen window and production startup hook; isolated fake platforms."""
import copy,hashlib,json,sqlite3,sys,time,traceback,threading
from contextlib import closing
from pathlib import Path
from PySide6.QtCore import QEventLoop,QTimer
from PySide6.QtWidgets import QDialog,QLabel,QPushButton
from browser_startup_fixture import AxFixture,JstFixture
from browser_startup_ui import start_application_connections
from version_info import diagnostic


def signature(path):
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)) as db:
        tables=[r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        digest=hashlib.sha256()
        for table in tables:
            quoted='"'+table.replace('"','""')+'"'
            rows=sorted(repr(r) for r in db.execute('SELECT * FROM '+quoted))
            digest.update((table+'\n'+'\n'.join(rows)).encode())
        return digest.hexdigest()


def exercise_binding_repair(app, win, jst, out, require, wait):
    startup = win.browser_startup
    owner = win.catalog_sessions.session
    jst.accounts = [a for a in jst.accounts if a['warehouse'] != '河北仓']
    jst.rebind_required['河北仓'] = True
    jst.binding_epochs['河北仓'] = 3
    jst.batches = [{'id': 'retained-qa', 'createdAt': '2026-11-21', 'purpose': 'default_catalog', 'items': [
        {'account': copy.deepcopy(jst.accounts[0]), 'stage': 'ACCEPTED',
         'native': {'nativeTaskRef': 'original-platform-task', 'submissions': 1}},
        {'account': {'warehouse': '河北仓', 'expectedCompany': '原隔离河北仓'}, 'stage': 'ACCEPTED',
         'retirement': {'reason': 'synthetic binding repair'}, 'native': {'nativeTaskRef': 'original-hb-task'}},
    ]}]
    original_accounts, original_batches = copy.deepcopy((jst.accounts, jst.batches))
    jst.calls.clear()
    startup.retry(['河北仓'])
    wait(lambda: not startup.workers)
    require(startup.records['河北仓']['state'] == 'CONFIG_REQUIRED' and startup.records['山东仓']['state'] == 'READY',
            'Only Hebei remains pending while Shandong stays verified')

    def panel():
        return next(d for d in app.topLevelWidgets() if isinstance(d, QDialog) and d.isVisible()
                    and d.windowTitle() == '连接检查与修复')

    def repair_button(dialog):
        return next(b for b in dialog.findChildren(QPushButton) if b.text() == '核验并保存')

    marked = []
    lease = owner.acquire()
    lease.start()
    def check_busy():
        dialog = panel()
        try:
            marked.append(not repair_button(dialog).isEnabled())
        finally:
            dialog.accept()
    try:
        QTimer.singleShot(50, check_busy)
        startup.open_details()
        require(marked == [True] and owner.lease is lease and not any(a in {'accounts.auto', 'sync.stop'} for a, _ in jst.calls),
                'Repair waits for the original catalogue lease without cancelling it')
    finally:
        lease.close()

    def enter_repair(save):
        completed = []
        deadline = time.monotonic() + 6
        def inspect_repair():
            dialog = getattr(win, '_large_io_dialog', None)
            if not getattr(dialog, 'binding_repair', False) or not dialog.probe:
                if time.monotonic() < deadline:
                    QTimer.singleShot(20, inspect_repair)
                    return
                for candidate in app.topLevelWidgets():
                    if getattr(candidate, 'binding_repair', False) and candidate.isVisible():
                        candidate.reject()
                completed.append('Timed out waiting for Hebei verification')
                return
            try:
                assert dialog.warehouse.currentText() == '河北仓' and not dialog.warehouse.isEnabled()
                assert dialog.manual.isHidden() and dialog.connection.isHidden()
                assert dialog.mapping.isVisible() and dialog.bind_btn.isVisible()
                assert not dialog.bind_btn.isEnabled() and '已核验公司 隔离河北仓' in dialog.identity.text()
                assert jst.accounts == original_accounts and jst.rebind_required['河北仓']
                if save:
                    dialog.mapping.setChecked(True)
                    assert dialog.bind_btn.isEnabled()
                    dialog.grab().save(str(out/'04-hebei-verified-before-save.png'))
                    dialog.bind_btn.click()
                completed.append(None)
            except Exception:
                completed.append(traceback.format_exc())
            finally:
                if dialog.isVisible():
                    dialog.reject()

        def open_repair():
            dialog = panel()
            try:
                action = repair_button(dialog)
                assert action.isEnabled()
                dialog.grab().save(str(out/'04-hebei-binding-repair-entry.png'))
                QTimer.singleShot(20, inspect_repair)
                action.click()
            except Exception:
                completed.append(traceback.format_exc())
            finally:
                if dialog.isVisible():
                    dialog.accept()
        QTimer.singleShot(60, open_repair)
        startup.open_details()
        wait(lambda: not startup.workers)
        if completed != [None]:
            raise AssertionError('Hebei repair UI failed: ' + repr(completed))
    enter_repair(save=False)
    require(jst.accounts == original_accounts and jst.rebind_required['河北仓']
            and not any(a == 'accounts.bind' for a, _ in jst.calls),
            'Closing the direct dialog after verification leaves the warehouse pending')
    enter_repair(save=True)
    require(startup.records['河北仓']['state'] == 'READY' and startup.records['山东仓']['state'] == 'READY',
            'Confirming the verified Hebei mapping from the main panel saves it and rechecks Hebei')
    require(jst.accounts[0:1] == original_accounts and jst.batches == original_batches,
            'Shandong binding and both original task records retain exact contents')
    warehouse_calls = [(action, values['warehouse']) for action, values in jst.calls
                       if action in {'accounts.auto', 'accounts.bind', 'accounts.ensure'}]
    require(warehouse_calls == [('accounts.auto', '河北仓'), ('accounts.auto', '河北仓'),
                                ('accounts.bind', '河北仓'), ('accounts.ensure', '河北仓')]
            and not any(a == 'sync.stop' for a, _ in jst.calls),
            'Repair only verifies and saves Hebei and never stops an earlier operation')


def start_smoke(app,win,args):
    out=Path(args[0]);app.setQuitOnLastWindowClosed(False)
    checks=[];beats=[];report={};startup=win.browser_startup;bar=win.browser_status
    def require(ok,text):
        if not ok:raise AssertionError(text)
        checks.append(text)
    def wait(check,seconds=15):
        until=time.monotonic()+seconds
        while not check() and time.monotonic()<until:
            loop=QEventLoop();QTimer.singleShot(15,loop.quit);loop.exec()
        if not check():raise AssertionError('Timed out waiting for isolated startup UI')
    def run():
        nonlocal report
        ax=AxFixture();jst=JstFixture();jst.delay=.04
        beat=QTimer(win);beat.setInterval(20);beat.timeout.connect(lambda:beats.append(time.monotonic()));beat.start()
        try:
            win.jst_generator.timer.stop();win.aoxiang_workflows.timer.stop();win.scheduled.timer.stop()
            wait(lambda:not win.jst_generator.worker and not bar.workers)
            axowner=win.aoxiang_workflows.session;jowner=win.catalog_sessions.session
            require(axowner.client is None and jowner.client is None,'Acceptance has no real platform transport')
            axowner.factory=lambda:ax;jowner.factory=lambda _:jst
            before=signature(win.work_repository.path)
            require(not (Path(win.repository.folder)/'analysis/stockout').exists(),'Analysis database remains absent before startup connection checks')
            ax.gate=threading.Event()
            start_application_connections(win,['补货助手.exe'])
            require(startup.started and set(startup.workers)=={'ax','jst'},'Normal launch hook starts both connection checks without a button click')
            wait(lambda:'jst' not in startup.workers)
            require('ax' in startup.workers and startup.records['河北仓']['state']=='READY','Slow AX connection does not block the two warehouse checks')
            require(win.scheduled.busy(),'A due schedule waits for remaining startup check instead of failing on session contention')
            require(len(beats)>5,'Main window continues processing events during connection startup')
            ax.gate.set();wait(lambda:not startup.workers)
            bar.refresh();wait(lambda:not bar.workers)
            require(all(r['state']=='READY' for r in startup.records.values()),'All three platforms complete login verification automatically')
            require(all('登录已核验' in x.text() for x in bar.labels.values()),'Footer distinguishes verified login from a live browser socket')
            require(axowner.client is ax and jowner.client is jst and ax.running and jst.running,'Successful startup retains the original shared clients')
            require(jowner.lease is None and win.aoxiang_workflows.gate.background is None,'Startup releases business operation ownership')
            win.resize(1200,850);app.processEvents();win.grab().save(str(out/'01-startup-ready.png'))
            # Exercise the actual main-window settings entry, including its return
            # callback. Directly constructing a dialog missed the old forced retry.
            saved_request=ax.request
            saved_config={'configured':True,'enabled':True,'display_name':'隔离商家','account_binding':{'merchant_code':'QA'},
                'browser_binding':{'edge_executable':'C:/QA/edge.exe','edge_version':'隔离验收','actual_profile_path':'C:/QA/Profile 1','profile_directory':'Profile 1','output_root':'C:/QA/downloads'},
                'targets':[{'buyerWarehouseCode':'QA1','displayName':'隔离门店','mappingVerifiedAt':'2026-11-08'}]}
            def saved_state(action,**values):
                if action=='configuration':ax.calls.append((action,values));return saved_config
                if action=='session.health':return {'state':'CONNECTED','login_state':'VERIFIED'}
                return saved_request(action,**values)
            ax.request=saved_state;settings_seen=[];settings_errors=[]
            before_checks=sum(a=='setup.check_login' for a,v in ax.calls)
            def inspect_and_close():
                dialog=getattr(win,'_aoxiang_dialog',None)
                if dialog is None or dialog.worker:
                    QTimer.singleShot(20,inspect_and_close);return
                try:
                    require(dialog.edge.currentData()=='C:/QA/edge.exe' and dialog.profile.currentData()=='C:/QA/Profile 1','Settings automatically displays the saved global browser and profile')
                    require('登录已核验' in dialog.status.text(),'Settings displays current shared login status without another browser probe')
                    dialog.data={'items':[{'code':'QA1','name':'隔离门店'}]};dialog.fill_directory()
                    require('已保存核验' in dialog.table.item(0,3).text(),'Saved store verification is restored rather than reset to unverified')
                    dialog.grab().save(str(out/'03-settings-restored.png'));settings_seen.append(True)
                except Exception:settings_errors.append(traceback.format_exc())
                finally:dialog.close()
            QTimer.singleShot(20,inspect_and_close);win.open_aoxiang_setup();app.processEvents()
            require(settings_seen==[True] and not settings_errors,'Actual settings entry opens and closes safely without edits')
            require(not startup.workers and sum(a=='setup.check_login' for a,v in ax.calls)==before_checks,'Closing settings does not trigger another login check')
            require(axowner.client is ax and ax.running and not win.aoxiang_workflows.gate.yielding,'Closing settings retains the original global connection and releases only operation ownership')
            from aoxiang_fetch_ui import FetchDialog
            fetch=FetchDialog(win,win.repository.folder,win.job_day);fetch.show();wait(lambda:fetch.worker is None)
            require(fetch.configuration['configured'] and fetch.stores.rowCount()==1 and axowner.client is ax,'Opening replenishment after settings close reuses the same saved account and store binding')
            fetch.close();wait(lambda:fetch._can_close and fetch.worker is None);fetch.deleteLater();ax.request=saved_request
            # Simulate a current-login component; Node setup/session tests cover
            # actual identity observation and atomic metadata update separately.
            from aoxiang_current_login import current_configuration
            from aoxiang_client import ComponentError
            observed=dict(saved_config,login_policy='current_edge',account_binding={'account_id':'OLD','merchant_code':'QA'})
            live={'account_id':'NEW-OPERATOR','merchant_code':'QA'}
            def current_login_request(action,**values):
                if action=='configuration':ax.calls.append((action,values));return observed.copy()
                if action=='setup.check_login':
                    require(not values,'Current login probe sends no manually configured account or merchant ID')
                    observed['account_binding']=live.copy()
                    observed['needs_store_verification']=live['merchant_code']!='QA'
                    observed['enabled']=not observed['needs_store_verification']
                    return {'id':'current-login','state':'VERIFIED','merchant_changed':observed['needs_store_verification'],
                            'identity':{'display_name':'隔离商家 · 当前登录人'}}
                return saved_request(action,**values)
            ax.request=current_login_request
            ticket=win.aoxiang_workflows.gate.request_foreground();lease=axowner.lease(ticket);lease.start()
            try:
                current=current_configuration(lease)
                require(current['account_binding']['account_id']=='NEW-OPERATOR' and current['targets']==saved_config['targets'],'New business reads current Edge login and keeps same-merchant stores')
                live['merchant_code']='OTHER-MERCHANT'
                try:current_configuration(lease)
                except ComponentError as error:require(error.code=='TARGET_UNVERIFIED','Different merchant connects but cannot reuse old merchant stores')
                else:raise AssertionError('Old store mappings must not be used for another merchant')
            finally:lease.close()
            require(axowner.client is ax and ax.running,'Observing a changed login reuses the original global component transport')
            live['merchant_code']='QA';startup.retry(['翱象']);wait(lambda:not startup.workers)
            require(startup.records['翱象']['state']=='READY' and axowner.client is ax,'Automatic startup recheck uses current Edge login on the same transport')
            ax.request=saved_request
            original_request=ax.request;settled=threading.Event();polled=threading.Event()
            def handoff_request(action,**values):
                if action=='setup.check_login':
                    ax.calls.append((action,values));return {'id':'handoff','state':'RUNNING'}
                if action=='setup.status':
                    polled.set();ax.calls.append((action,values));return {'id':'handoff','state':'VERIFIED' if settled.is_set() else 'RUNNING'}
                return original_request(action,**values)
            ax.request=handoff_request;startup.retry(['翱象']);wait(polled.is_set)
            ticket=win.aoxiang_workflows.gate.request_foreground();settled.set();wait(lambda:not startup.workers)
            lease=axowner.lease(ticket);lease.start()
            require(axowner.client is ax and ax.running and not ax.stopped,'Foreground handoff keeps original transport and never cancels the in-flight startup read')
            require(lease.close(),'Foreground lease safely releases after handoff')
            ax.request=original_request;startup.retry(['翱象']);wait(lambda:not startup.workers)
            ax.request=lambda action,**kw:({'state':'CONNECTED','login_state':'FAILED','login_error':{'code':'IDENTITY_CHECK_FAILED','message':'身份查询失败，尚不能判断登录失效'}} if action=='session.health' else original_request(action,**kw))
            bar.refresh();wait(lambda:not bar.workers)
            require(startup.records['翱象']['state']=='FAILED' and '登录已核验' not in bar.labels['翱象'].text(),'Business identity failure clears stale verified footer while retaining the live socket')
            win.grab().save(str(out/'02-identity-query-failed.png'))
            ax.request=original_request;startup.retry(['翱象']);wait(lambda:not startup.workers)
            jst.errors['山东仓']={'code':'LOGIN_REQUIRED','message':'本仓登录已过期'}
            jst.errors['河北仓']={'code':'WRONG_ACCOUNT','message':'聚水潭当前操作员与本仓绑定账号不一致。'}
            startup.retry(['山东仓','河北仓']);wait(lambda:not startup.workers);bar.refresh();wait(lambda:not bar.workers)
            require('需要登录' in bar.labels['山东仓'].text() and '账号不符' in bar.labels['河北仓'].text(),'Live sockets with expired or wrong accounts are not shown as login verified')
            captured=[]
            def capture_dialog():
                for dialog in app.topLevelWidgets():
                    if isinstance(dialog,QDialog) and dialog.windowTitle()=='连接检查与修复':
                        messages = [label.text() for label in dialog.findChildren(QLabel)]
                        captured.append(any(jst.errors['河北仓']['message'] in text for text in messages)
                                        and dialog.grab().save(str(out/'02-login-repair.png')))
                        dialog.accept()
            QTimer.singleShot(150,capture_dialog);startup.open_details()
            require(captured==[True],'Actual repair dialog displays reasons and per-platform retry actions')
            jst.calls.clear();jst.errors.pop('山东仓');startup.retry_pending();wait(lambda:not startup.workers)
            require(startup.records['山东仓']['state']=='READY','Completing login recovers automatically without another connect click')
            require([v['warehouse'] for a,v in jst.calls if a=='accounts.ensure']==['山东仓'],'Automatic retry only checks the login-required warehouse')
            jst.errors.clear();startup.retry(['河北仓']);wait(lambda:not startup.workers)
            require(startup.records['河北仓']['state']=='READY','Manual diagnostic retry can recover the remaining warehouse')
            require(not (Path(win.repository.folder)/'analysis/stockout').exists() and win.stockout.page is None,'Startup does not create or load the stockout business database or page')
            require(not any(n in sys.modules for n in ['stockout_repository','stockout_ui','stockout_facts']),'Startup avoids heavy stockout module imports')
            require(signature(win.work_repository.path)==before,'All original business tables retain exact logical contents')
            require(all(a in {'configuration','setup.check_login','setup.status','session.release','session.health'} for a,v in ax.calls),'AX startup issues no store scans, exports, purchases or setup commits')
            require(all(a in {'sync.status','accounts.ensure','session.release','session.health'} for a,v in jst.calls),'Warehouse startup issues no catalogue export or adoption')
            exercise_binding_repair(app,win,jst,out,require,wait)
            require(signature(win.work_repository.path)==before,'Direct account repair leaves the order and draft database unchanged')
            # Closing with a read-only check in flight must drain it before window disposal.
            axowner.close();ax.gate=threading.Event();startup.retry(['翱象']);wait(lambda:ax.running)
            win.close();require(win.isVisible() and startup.closing,'Closing during startup first requests cooperative cancellation')
            ax.gate.set();wait(lambda:not win.isVisible() and not startup.workers)
            require(not ax.running and not jst.running,'Actual window close drains both retained component owners')
            report={'passed':True,**diagnostic(),'frozen_exe':bool(getattr(sys,'frozen',False)),'checks':checks,
                    'heartbeat_count':len(beats),'business_signature':before,'scope':'actual startup hook and direct warehouse repair in Qt with isolated platform fixtures'}
        except Exception:
            report={'passed':False,'checks':checks,'error':traceback.format_exc(),'frozen_exe':bool(getattr(sys,'frozen',False))}
        finally:
            if ax.gate:ax.gate.set()
            startup.prepare_close();bar.prepare_close();wait(lambda:not startup.workers and not bar.workers)
            beat.stop();(out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n','utf-8')
            win._discard_close=True;win.close();QTimer.singleShot(250,lambda:app.exit(0 if report.get('passed') else 1))
    QTimer.singleShot(200,run)
