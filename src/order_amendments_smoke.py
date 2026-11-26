"""Exercise the real edit/compare/confirm dialogs on an isolated database."""
from contextlib import closing
import json
import os
from pathlib import Path
import traceback
from types import SimpleNamespace
from unittest.mock import patch

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QInputDialog, QPushButton, QMessageBox


def start_smoke(app, win, args):
    root=Path(args[0]).resolve();phase=args[1]
    fixture=json.loads((root/'amendment-fixture.json').read_text('utf-8'))
    if (fixture['synthetic'] is not True or phase not in ('exercise','reopen') or
            Path(os.environ.get('REPLENISHMENT_DATA_DIR','')).resolve()!=root/'settings' or
            win.work_repository.path.resolve()!=root/'settings/workbench.sqlite3'):
        raise ValueError('手动更正验收须在隔离资料目录运行')
    win.auto_check.stop();win.jst_generator.stopped=True
    win.browser_status.paused=True;win.browser_status.initial.stop()
    for service in (win.aoxiang_workflows,win.jst_generator,win.jst_fulfillment_runner,win.browser_status,win.scheduled):
        service.timer.stop()
    QTimer.singleShot(220,lambda:exercise(app,win,root,phase,fixture))


def exercise(app,win,root,phase,fixture):
    import order_lifecycle as lifecycle
    from lifecycle_ui import LifecycleDialog
    from order_amendments_ui import Editor
    from lifecycle_io import TITLES
    import normal_orders
    import purchases
    import warehouse_execution
    from version_info import diagnostic
    from jst_order_store import active, read_generation
    import work_repository as wr
    import sys

    path=win.work_repository.path;stage=fixture['stage'];head=fixture['head'];other=fixture['other']
    report={'passed':False,'actual_exe':bool(getattr(sys,'frozen',False)),'build':diagnostic(),
            'warehouse':head['warehouse'],'stage':stage,'synthetic':True}
    dialog=None;timer=QTimer();entered=set();errors=[]

    def watch():
        current=app.activeModalWidget()
        if current is None:return
        try:
            if isinstance(current,Editor) and 'editor' not in entered:
                entered.add('editor')
                pasted=f"{fixture['sku']}\t{fixture['quantity']}\t测试产品"+ ('\t22.20' if stage=='payment' else '')
                with patch.object(QApplication,'clipboard',return_value=SimpleNamespace(text=lambda:pasted)):
                    current.paste()
                current.operator.setText('隔离经办');current.reason.setText('核对实际数量')
                app.processEvents()
                assert current.grab().save(str(root/'更正编辑.png'))
                QTimer.singleShot(0,current.review.click)
            elif current.objectName()=='interactionReview' and 'review' not in entered:
                entered.add('review')
                assert current.grab().save(str(root/'更正前后.png'))
                next(btn for btn in current.findChildren(QPushButton) if btn.text()=='确认保存更正').click()
        except Exception:
            errors.append(traceback.format_exc());current.reject()

    def warning(*args):
        errors.append(str(args[2]))
        current=app.activeModalWidget()
        if isinstance(current,Editor):current.reject()

    try:
        if phase=='exercise':
            dialog=LifecycleDialog(win,win,head);dialog.show();app.processEvents()
            assert dialog.manual_edit.isEnabled()
            timer.timeout.connect(watch);timer.start(30)
            title=TITLES.get(stage,{'allocation':'库存分配','signed':'本地签发'}.get(stage))
            with (patch.object(QInputDialog,'getItem',return_value=(title,True)),
                  patch.object(QMessageBox,'information'),
                  patch.object(QMessageBox,'warning',side_effect=warning)):
                dialog.manual_edit.click()
            assert not errors and entered=={'editor','review'},errors
        if stage=='allocation':
            with closing(wr.read_connection(path)) as db:
                gid=active(db,head); sibling=active(db,other); current,digest=read_generation(db,gid)
                event=db.execute("SELECT count(*) FROM jst_events WHERE action='手动更正分配' AND oid=? AND warehouse=?",
                                 (head['oid'],head['warehouse'])).fetchone()[0]
                row=next(r for r in current['rows'] if r['sku']==fixture['sku'])
            assert gid!=fixture['prior_id'] and sibling==fixture['other_id'] and event==1
            assert row['quantity']==fixture['quantity']
            report.update(previous=fixture['prior_id'],current=gid,sibling=sibling,digest=digest,rows=current['manual_correction']['changes'])
        elif stage=='signed':
            live={h['warehouse']:h for h in purchases.targets(path,current_only=True)}
            after=live[head['warehouse']]
            assert after['revision']==head['revision']+1 and live[other['warehouse']]['digest']==fixture['other_id']
            assert lifecycle.full_head(path,after)['products'][fixture['sku']]['quantity']==int(fixture['quantity'])
            assert lifecycle.full_head(path,head)['digest']==fixture['prior_id']
            saved=warehouse_execution.get_order(normal_orders.read_repository(path),after)['payload']['result']['signed_correction']
            assert saved['operator']=='隔离经办'
            report.update(previous=fixture['prior_id'],current=after['revision'],sibling=fixture['other_id'],
                          digest=after['digest'],rows=saved['changes'])
        else:
            docs=lifecycle.history(path,head,stage)
            assert len(docs)==2 and docs[1]['digest']==fixture['prior_id']
            assert lifecycle.history(path,other,stage)[0]['digest']==fixture['other_id']
            assert docs[0]['payload']['manual_correction']['operator']=='隔离经办'
            row=next(r for r in docs[0]['payload']['rows'] if r['sku']==fixture['sku'])
            assert row['base_quantity']==fixture['quantity']
            report.update(previous=fixture['prior_id'],current=docs[0]['version'],sibling=fixture['other_id'],
                          digest=docs[0]['digest'],rows=docs[0]['payload']['manual_correction']['changes'])
        report['passed']=True
    except Exception:
        report['error']=traceback.format_exc()
    finally:
        timer.stop()
        if dialog is not None:dialog.close()
        win.job_dirty=False;win._discard_close=True;win.close()
        (root/(phase+'-report.json')).write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
        QTimer.singleShot(80,lambda:app.exit(0 if report['passed'] else 1))
