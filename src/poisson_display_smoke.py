"""Opt-in acceptance of the packaged calculation, history and export widgets."""
from pathlib import Path
import json
import os
import time
import traceback
from unittest.mock import patch

from PySide6.QtCore import QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QPushButton

import export_locations
import result_semantics as sem
from version_info import diagnostic

FAST = '5003822828611'


def start_smoke(app, win, args):
    case_file = Path(args[0]).resolve()
    root = case_file.parent
    plan = json.loads(case_file.read_text('utf-8'))
    settings = (root/'settings').resolve()
    if Path(os.environ.get('REPLENISHMENT_DATA_DIR','')).resolve() != settings or Path(app.data_root).resolve() != settings:
        raise ValueError('验收必须使用用例内的独立资料目录')
    if plan.get('fixture') != 'poisson-display-rc16':
        raise ValueError('缺少隔离验收用例标记')
    timer = QTimer(win)
    start = time.monotonic()
    state = {'step':'start','checks':[],'shown':{}}
    store = plan['store']

    def click(button):
        assert button.isEnabled(), button.text()
        QTest.mouseClick(button,Qt.MouseButton.LeftButton)

    def save_picture(widget, name):
        widget.repaint()
        assert widget.grab().save(str(root/(name+'.png')))

    def checked(text):
        state['checks'].append(text)

    def finish(error=None):
        timer.stop()
        report={'passed':error is None,'error':error,'step':state['step'],'version':diagnostic(),
                'checks':state['checks'],'visible_data':state['shown']}
        (root/'gui-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        win.job_dirty=False;win._discard_close=True
        dialog=state.get('dialog')
        if dialog:dialog.reject()
        if not win.worker:win.close()
        app.exit(0 if report['passed'] else 1)

    def report_rows(rows):
        with (root/'live-result.jsonl').open('w',encoding='utf-8') as output:
            for row in rows:
                data={key:row.get(key) for key in ('E','AU','AS','AQ','AL','BF','BG','AW','K','L')}
                data.update(source_row=row['_meta'].get('export_row'),qty=sem.order_quantity(row),
                            amount=sem.order_amount(row),execution_state=sem.order_status(row))
                output.write(json.dumps(data,ensure_ascii=False,allow_nan=False,default=str)+'\n')

    def export_button(button, path):
        with patch.object(export_locations,'save_file',return_value=(str(path),'Excel (*.xlsx)')):
            click(button)

    def export_archive(head):
        from archive_batch_ui import Preview
        folder=root/'完整决策导出';folder.mkdir()
        confirmations=QTimer(win);errors=[]
        def respond():
            modal=QApplication.activeModalWidget()
            if isinstance(modal,Preview):
                save_picture(modal,'签发导出范围')
                click(modal.start)
            elif isinstance(modal,QMessageBox):
                errors.append(modal.text());modal.reject()
            elif isinstance(modal,QDialog) and modal.windowTitle()=='导出结果 · 原归档未改变':
                save_picture(modal,'签发导出结果');modal.accept()
        confirmations.timeout.connect(respond);confirmations.start(150)
        try:
            with patch.object(export_locations,'directory',return_value=str(folder)):
                win.export_archive(head)
        finally:confirmations.stop()
        assert not errors,errors
        files=list(folder.rglob('*.xlsx'));assert len(files)==1,files
        state['shown']['签发决策依据文件']=str(files[0])

    def tick_live():
        if state['step']=='start':
            win.show_section(1);win.navigate(0)
            win.sales.add_paths([plan['sales']]);state['step']='intake';click(win.load_btn)
        elif state['step']=='intake' and not win.worker and win.loaded:
            assert list(win.loaded['stores'])==[store]
            win.navigate(1)
            settings = win.selected_settings(store)
            assert tuple(settings['warehouses']['山东仓'])==tuple(plan['cycle']),settings['warehouses']
            state['shown']['cycle']=list(settings['warehouses']['山东仓'])
            save_picture(win,'01-计算参数')
            state['step']='calculate';click(win.calculate_btn)
        elif state['step']=='calculate' and not win.worker and store in win.results:
            assert win.states[store]['state']=='已完成',win.states[store]
            result=win.results[store];rows=result['rows'];known=[sem.order_quantity(r) for r in rows]
            assert len(rows)==22323 and sum(sem.numeric(q) for q in known)==8618
            assert sum(q for q in known if sem.numeric(q))==plan['expected_total']
            report_rows(rows);checked('通过计算按钮取得全店结果，留存全部22323行')
            win.open_overview(win.overview_codes.index(store),0)
            win.search.setText(FAST);win.filter_changed()
            assert win.proxy.rowCount()==1
            index=win.proxy.index(0,0);win.table.scrollTo(index)
            click_point=win.table.visualRect(index).center()
            QTest.mouseClick(win.table.viewport(),Qt.MouseButton.LeftButton,pos=click_point)
            if not win.detail_panel.isVisible():win.show_detail(index)
            note=win.details.toPlainText()
            assert '0.3.7-rules' in note and '趋势调整前需求 AO' in note,note
            assert '− 可用库存 3 − 在途库存 0' in note,note
            fast=next(d for d in rows if d['E']==FAST)
            assert (fast['K'],fast['L'],sem.order_quantity(fast))==(3,0,2)
            state['shown']['快充线详情']=note
            save_picture(win,'02-快充线详情')
            win.show_technical_detail(fast)
            tech=win.details.toPlainText()
            assert '0.3.7-rules' in tech and '趋势调整前需求 AO' in tech and '− 可用库存 3 − 在途库存 0' in tech,tech
            state['shown']['快充线完整技术依据']=tech
            save_picture(win,'03-快充线技术依据')
            state['step']='export';export_button(win.export_all,root/'单店全量明细.xlsx')
        elif state['step']=='export' and not win.worker and (root/'单店全量明细.xlsx').is_file():
            checked('单店全量明细通过界面导出')
            finish()

    def form_values(dialog):
        return {r[0]:r[1:] for r in dialog.model.rows}

    def check_form(case, values):
        version,basis=values['模型版本'][0],values['泊松均值依据'][0]
        assert (version,basis)==(plan['model_version'],plan['mean_basis']),values
        q,net=values['泊松分位数 Q'][0],values['净需求']
        assert q==case['quantile'] and net[0]==case['net'],values
        for text in case['reason']:
            assert text in net[-1],(text,net)
        state['shown'][case['sku']+' / '+state['scope']]=values

    def tick_history():
        from history_read import detail_head
        from history_ui import AuditDialog
        if state['step']=='start':
            head=detail_head(win.work_repository.path,plan['day'],store,'山东仓',1)
            assert head['number']==plan['number'] and head['revision']==1,head
            state['head']=head;state['case']=0;state['step']='open'
        elif state['step']=='open':
            case=plan['products'][state['case']]
            dialog=AuditDialog(win,win,state['head'],sku=case['sku'])
            dialog.setModal(False);dialog.show()
            state.update(dialog=dialog,scope='决策总览',step='form')
        elif state['step']=='form':
            dialog=state['dialog'];case=plan['products'][state['case']]
            if dialog.loaded_key!=(state['scope'],case['sku']):return
            values=form_values(dialog);check_form(case,values)
            save_picture(dialog,f'{case["sku"]}-{state["scope"]}')
            if state['scope']=='决策总览':
                assert values['最终签发量'][0]==case['quantity'],values
                state['step']='form-export'
                export_button(dialog.export_btn,root/(case['sku']+'-决策总览.xlsx'))
            else:
                dialog.reject();dialog.deleteLater();state.pop('dialog')
                checked(case['sku']+' 两种历史详情和表单导出一致')
                state['case']+=1
                state['step']='open' if state['case']<len(plan['products']) else 'archive'
        elif state['step']=='form-export':
            dialog=state['dialog'];case=plan['products'][state['case']]
            if not (root/(case['sku']+'-决策总览.xlsx')).is_file() or not dialog.export_btn.isEnabled():return
            state.update(scope='商品完整计算依据',step='form')
            dialog.scope.setCurrentText(state['scope'])
        elif state['step']=='archive':
            if not plan.get('archive_excel'):
                finish()
                return
            export_archive(state['head'])
            checked('通过历史签发入口导出完整决策依据')
            finish()

    def tick():
        if state.get('handling'):return
        if time.monotonic()-start>480:
            finish('验收未完成 '+state['step'])
            return
        modal=QApplication.activeModalWidget()
        if isinstance(modal,QMessageBox):
            button=next((b for b in modal.buttons() if b.text() in ('完成','知道了','确定','OK')),None)
            if modal.windowTitle() in ('导出完成','业务表单已导出','归档资料导出完成') and button:
                click(button)
                return
            finish('发生意外提示 '+modal.windowTitle()+' '+modal.text())
            return
        state['handling']=True
        try:
            tick_live() if plan['kind']=='live' else tick_history()
        except Exception:
            finish(traceback.format_exc())
        finally:state['handling']=False

    win.failed=lambda message:finish(str(message))
    timer.timeout.connect(tick)
    timer.start(120)
