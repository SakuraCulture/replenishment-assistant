"""Verify report actions, a timed occurrence, and recovery on isolated data."""
from contextlib import closing
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import traceback
from unittest.mock import patch

from PySide6.QtCore import QDate, QTime, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QFileDialog
from openpyxl import load_workbook

from export_files import sha256
from joint_export_smoke import isolated, press, wait, write
from timed_workbench_ui import Materials, PlanEditor
from business_daily_sources import template_weeks


def business_facts(path):
    digest = hashlib.sha256()
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)) as db:
        tables = db.execute('SELECT name FROM sqlite_master WHERE type=\'table\' ORDER BY name')
        for name, in tables:
            if name != 'normal_events' and name.startswith(('normal_', 'warehouse_execution_', 'lifecycle_', 'jst_', 'purchase')):
                digest.update(name.encode())
                for row in db.execute(f'SELECT * FROM "{name}" ORDER BY rowid'):
                    for value in row:
                        data = value if isinstance(value, bytes) else repr(value).encode('utf-8')
                        digest.update(len(data).to_bytes(8, 'little'))
                        digest.update(data)
    return digest.hexdigest()


def compare(report_a, report_b):
    a, b = Path(report_a['path']), Path(report_b['path'])
    names = ['汇总', '门店缺货率', '门店缺货明细', '口径说明']
    for values_only in (False, True):
        options = {'read_only': True, 'keep_links': False, 'data_only': values_only}
        with closing(load_workbook(a, **options)) as first, closing(load_workbook(b, **options)) as second:
            assert first.sheetnames == names == second.sheetnames
            for name in names:
                assert list(first[name].values) == list(second[name].values), (name, values_only)


def start_smoke(app, win, args):
    root, mode = Path(args[0]).resolve(), args[1]
    isolated(win, root)
    settings = json.loads((root/'fixture.json').read_text('utf-8'))
    manager = win.timed_workbench
    manager.timer.stop()
    day = settings['day']
    clock = [datetime.fromisoformat(settings['clock'])]
    manager.repo.clock = lambda: clock[0]

    def exercise():
        report = {'passed': False, 'frozen_exe': bool(getattr(sys,'frozen',False)), 'live_platform': False, 'runs': []}
        reports = report['runs']
        timer = QTimer(win)
        timer.setInterval(20)
        errors = []
        probe = None
        probe_log = None
        report_windows = []
        original_render = manager.backend.render

        def render(*args, **kwargs):
            started = time.time()
            result = original_render(*args, **kwargs)
            report_windows.append({'type':args[0]['plan']['type'],'from_ms':round(started*1000),'to_ms':round(time.time()*1000)})
            return result

        def modal(action, dialog_type, prepare):
            done = []
            def inspect():
                active = app.activeModalWidget()
                if active is None:
                    return
                timer.stop()
                try:
                    assert isinstance(active,dialog_type), active.windowTitle()
                    prepare(active)
                    done.append(True)
                except Exception:
                    errors.append(traceback.format_exc())
                if active.isVisible():
                    active.reject()
            timer.timeout.connect(inspect)
            timer.start()
            try:
                action()
            finally:
                timer.stop()
                timer.timeout.disconnect(inspect)
            assert done and not errors, errors

        def configure(dialog, kind, timed=False):
            dialog.kind.setCurrentIndex(dialog.kind.findData(kind))
            dialog.rule.setCurrentIndex(dialog.rule.findData('fixed'))
            selected_day = settings.get('sku_day', day) if kind=='sku_completion' else day
            for field in (dialog.start, dialog.end):
                field.setDate(QDate.fromString(selected_day,'yyyy-MM-dd'))
            dialog.partial.setChecked(kind in ('business_daily','stockout'))
            dialog.folder.setText(str(root/'export'))
            dialog.clock.setTime(QTime(9,1))
            dialog.refresh_preview()
            assert '09:01' in dialog.note.toPlainText()
            dialog.grab().save(str(root/('plan-' + kind + ('-timer' if timed else '') + '.png')))
            press(dialog, '保存并启用' if timed else '立即导出')
            assert dialog.result() == dialog.DialogCode.Accepted, dialog.note.toPlainText()

        def poll():
            if not manager.worker and not manager.reader:
                manager.poll()

        def run_result(rid, expected):
            def done():
                poll()
                row = manager.repo.inspect(rid)[0]
                state = row['state']
                if mode=='parallel' and state=='WAITING' and expected!='WAITING':
                    # Preserve the frozen business day while allowing the real wait clock to advance.
                    clock[0]=max(clock[0],datetime.fromisoformat(row['retry_at']))
                    QTest.qWait(300)
                return state == expected or state in ('REVIEW','EMPTY','PARTIAL','COMPLETE')
            wait(done,180)
            row, artifact = manager.repo.inspect(rid)
            assert row['state'] == expected, row['message']
            if artifact:
                assert sha256(artifact['path']) == artifact['sha256']
            reports.append({'run': row, 'artifact': artifact})
            wait(lambda:manager.worker is None and manager.reader is None,90)
            return row,artifact

        def visible(rid):
            manager.selected_run = rid
            page = manager.open()
            return page

        try:
            before = business_facts(win.work_repository.path)
            assert manager.host.scheduled.owner
            if mode == 'parallel':
                from PySide6.QtWidgets import QCheckBox
                from catalog_sync_client import components
                from types import SimpleNamespace
                from unattended_resources import configured
                page = manager.open()
                checkbox = next(widget for widget in page.findChildren(QCheckBox) if '独立浏览器与本地作业并行' in widget.text())
                checkbox.click()
                assert configured(manager.root)
                page.tabs.setCurrentIndex(2)
                page.grab().save(str(root/'unattended-queue.png'))
                page.close()
                jst, ax = components()
                probe_path = root/'browser-probe'
                script=Path(settings['browser_harness']).resolve(strict=True)
                assert script.name=='verify_unattended_browsers.mjs' and jst.parent==ax.parent
                probe_log = (root/'browser.log').open('wb')
                probe = subprocess.Popen([str(ax/'runtime/node.exe'),str(script),str(probe_path),str(ax.parent),'--hold','--one'],
                                         stdout=probe_log,stderr=probe_log,creationflags=subprocess.CREATE_NO_WINDOW)
                wait(lambda:(probe_path/'active.json').is_file() or probe.poll() is not None,75)
                assert probe.poll() is None
                # The local-only platform adapter owns the background step while the normal
                # workbench and report writers remain in the delivered application process.
                win.scheduled.worker=SimpleNamespace(probe_pid=probe.pid)
                manager.backend.render=render
            if mode == 'reopen':
                previous = json.loads((root/'exercise.json').read_text('utf-8'))
                assert previous['passed'] and before == previous['business_facts']
                target = previous['interrupted']
                page = visible(target)
                press(page, '继续原运行')
                _, artifact = run_result(target, 'PARTIAL')
                assert artifact['receipt_repaired'] and artifact['sha256'] == previous['interrupted_sha256']
                assert Path(artifact['path']).stat().st_mtime_ns == previous['interrupted_mtime_ns']
                page.grab().save(str(root/'receipt-repaired.png'))
                report['existing_file_retained'] = True
            else:
                press(win,'缺货率')
                wait(lambda:win.stockout_hub.page is not None)
                hub = win.stockout_hub.page
                hub.background.setChecked(True)
                press(win,'首页')
                press(win,'缺货率')
                hub = win.stockout_hub.page
                assert hub.background.isChecked()
                modal(lambda:press(hub,'门店缺货率日报（经营版）'),PlanEditor,
                      lambda editor:configure(editor,'business_daily'))
                manual = manager.selected_run
                defaults = template_weeks(day)
                if defaults:
                    manual_run, manual_artifact = run_result(manual,'PARTIAL')
                else:
                    waiting,_ = run_result(manual,'WAITING')
                    assert '周值' in waiting['message'] and not waiting['frozen']
                page = visible(manual)
                if not defaults:
                    page.grab().save(str(root/'waiting.png'))
                def supply(materials):
                    if settings.get('verify_added_sources'):
                        assert '已引用门店' in materials.files['stores'].text()
                        for key in ('stores', 'blacklist', 'owners'):
                            assert '起采用' in materials.files[key].text(), key
                    if defaults:
                        assert '目标已采用' in materials.files['weeks'].text()
                    else:
                        with patch.object(QFileDialog,'getOpenFileName',return_value=(settings['weeks'], '')):
                            materials.choose('weeks')
                        assert '待登记' != materials.files['weeks'].text()
                    materials.grab().save(str(root/'daily-sources.png'))
                    press(materials,'完成')
                modal(lambda:press(page,'补日报资料'),Materials,supply)
                clock[0] += timedelta(seconds=17)
                if not defaults:
                    manual_run, manual_artifact = run_result(manual,'PARTIAL')
                else:
                    assert manual_run['frozen']['week_defaults'] == defaults
                    with closing(load_workbook(manual_artifact['path'], read_only=True, data_only=True)) as workbook:
                        summary = workbook['汇总']
                        for row, metric in zip((3,8,13), defaults['metrics'].values()):
                            assert summary[f'C{row}'].value == metric['previous']
                            assert summary[f'D{row}'].value == metric['target']
                            assert [summary.cell(row+2, col).value for col in range(2,6)] == metric['weekly_targets']
                            assert all(summary.cell(row+3, col).value is None for col in range(2,6))
                    report['template_targets_applied'] = defaults
                page = visible(manual)
                page.tabs.setCurrentIndex(1)
                modal(lambda:press(page,'新建计划'),PlanEditor,lambda editor:configure(editor,'business_daily',True))
                plan, = manager.repo.specs()
                assert plan['enabled'] and plan['next_at']
                page.showMinimized()
                clock[0] = clock[0].replace(hour=9,minute=1,second=1)
                manager.poll()
                wait(lambda:any(r['requested_by']=='timer' for r in manager.repo.runs()))
                timed, = [r for r in manager.repo.runs() if r['requested_by']=='timer']
                timed_run, timed_artifact = run_result(timed['id'],'PARTIAL')
                assert timed_run['dates'] == manual_run['dates']
                assert len([r for r in manager.repo.runs() if r['requested_by']=='timer'])==1
                assert all(timed_run['frozen'][k]==v for k,v in manual_run['frozen'].items() if k!='frozen_at')
                compare(manual_artifact,timed_artifact)
                if settings.get('verify_added_sources'):
                    source_report = {'store_rules': manual_run['frozen']['store_rules'],
                                     'effective_days': {key: manual_run['frozen']['files'][key]['effective_day']
                                                        for key in ('stores', 'blacklist', 'owners')}}
                    for preview in settings['source_previews']:
                        preview_day = preview['day']
                        dialog = Materials(page, manager.root, preview_day)
                        dialog.show()
                        coverage = dialog.coverage.text()
                        assert coverage.count(preview['catalog_status']) == 2, coverage
                        if preview.get('has_store_rules'):
                            assert '已引用门店' in dialog.files['stores'].text()
                        dialog.grab().save(str(root/('daily-sources-' + preview_day + '.png')))
                        dialog.close()
                        dialog.deleteLater()
                        source_report[preview_day] = coverage
                    report['automatic_sources'] = source_report
                page.showNormal()
                page.tabs.setCurrentIndex(1)
                page.plans.selectRow(0)
                press(page,'停用未来计划')
                assert not manager.repo.specs()[0]['enabled']
                report['manual_and_timer_equal'] = True

                for kind,state in (('stockout','PARTIAL'),('sku_completion','COMPLETE')):
                    page.tabs.setCurrentIndex(0)
                    modal(lambda:press(page,'新建立即导出'),PlanEditor,lambda editor:configure(editor,kind))
                    run_result(manager.selected_run,state)

                with patch.object(manager.repo,'confirmed',side_effect=OSError('验收注入：已发布，回执未留存')):
                    modal(lambda:press(page,'新建立即导出'),PlanEditor,lambda editor:configure(editor,'business_daily'))
                    interrupted = manager.selected_run
                    interrupted_run, artifact = run_result(interrupted,'REVIEW')
                assert '验收注入' in interrupted_run['message'] and not artifact.get('published_at')
                report.update(interrupted=interrupted, interrupted_sha256=artifact['sha256'],
                              interrupted_mtime_ns=Path(artifact['path']).stat().st_mtime_ns,
                              leave_and_restore_background=True)
                page.grab().save(str(root/'publication-recovery.png'))
            assert before == business_facts(win.work_repository.path)
            if probe is not None:
                win.scheduled.worker=None
                (root/'browser-probe'/'release').write_text('release original fixture queries','ascii')
                wait(lambda:probe.poll() is not None,35)
                assert probe.returncode==0
                browser=json.loads((root/'browser-probe'/'report.json').read_text('utf-8'))
                common_start=max(row['from_ms'] for row in browser['intervals'] if row['name']=='initial')
                common_end=min(row['to_ms'] for row in browser['intervals'] if row['name']=='initial')
                assert {row['type'] for row in report_windows}=={'sku_completion','stockout','business_daily'}
                assert all(common_start<=row['from_ms']<=row['to_ms']<=common_end for row in report_windows)
                report['browser_and_export']={'browser_interval_ms':[common_start,common_end],'render_intervals':report_windows,
                                              'production_platform_submissions':0,'controlled_catalog_submissions':browser['counts']}
            report.update(passed=True,business_facts=before)
        except Exception:
            report['error'] = traceback.format_exc()
            report['modal_errors'] = errors
        finally:
            if probe is not None:
                win.scheduled.worker=None
                if probe.poll() is None and (root/'browser-probe').is_dir():
                    (root/'browser-probe'/'release').write_text('release original fixture queries','ascii')
                if probe_log:probe_log.close()
            timer.stop()
            write(root/(mode+'.json'), report)
            win.job_dirty=False
            win._discard_close=True
            win.close()
    QTimer.singleShot(200,exercise)
