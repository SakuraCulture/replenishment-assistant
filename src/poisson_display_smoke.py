"""Opt-in acceptance of the packaged calculation, history and export widgets."""
from hashlib import sha256
from math import isclose
from pathlib import Path
import json
import os
import time
import traceback
from unittest.mock import patch

from PySide6.QtCore import QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

import export_locations
import normal_exports
import result_semantics as sem
from version_info import diagnostic


def row_digest(value):
    encoded=json.dumps(value,ensure_ascii=False,sort_keys=True,default=str,allow_nan=False,separators=(',',':'))
    return sha256(encoded.encode()).hexdigest()


def start_smoke(app, win, args):
    case_file = Path(args[0]).resolve()
    root = case_file.parent
    plan = json.loads(case_file.read_text('utf-8'))
    settings = (root/'settings').resolve()
    if Path(os.environ.get('REPLENISHMENT_DATA_DIR','')).resolve() != settings or Path(app.data_root).resolve() != settings:
        raise ValueError('验收必须使用用例内的独立资料目录')
    isolation=json.loads((root/'synthetic.json').read_text('utf-8'))
    if plan.get('fixture') != 'poisson-display-rc17' or isolation != {'synthetic':True,'live_platform':False}:
        raise ValueError('缺少隔离验收用例标记')
    timer = QTimer(win)
    start = time.monotonic()
    state = {'step':'start','checks':[],'shown':{}}
    store = plan['store']

    def click(button):
        assert button.isEnabled(), button.text()
        QTest.mouseClick(button,Qt.MouseButton.LeftButton)

    def save_picture(widget, name):
        QApplication.processEvents()
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
        fields='D E G H I J K L N V U AB AC AD AG AH AN AM AO AP AU BD BE BF BG AL AQ BJ AS BM AW'.split()
        unchanged='A B C D E F G H I J K L M N O P Q R S T U V AB AC AD AE AG AH AI AJ AN AM AO AP AU AZ BA BD BH BI BL BN'.split()
        with (root/'live-result.jsonl').open('w',encoding='utf-8') as output:
            for row in rows:
                data={key:row.get(key) for key in fields}
                data.update(source_row=row['_meta'].get('export_row'),qty=sem.order_quantity(row),
                            amount=sem.order_amount(row),execution_state=sem.order_status(row),
                            excluded=sem.exclusion_reason(row),reason=normal_exports.decision(row),
                            p=row.get('_effective_probability'),target=sem.pre_round_target(row),
                            unaffected_sha256=row_digest(([row.get(key) for key in unchanged],sem.routing(row))),
                            flat_row_sha256=row_digest(row) if row.get('AM')==1 else None)
                output.write(json.dumps(data,ensure_ascii=False,allow_nan=False,default=str)+'\n')

    def show_live_product(case):
        sku=case['sku']
        win.search.setText(sku);win.filter_changed()
        assert win.proxy.rowCount()==1,sku
        index=win.proxy.index(0,0);win.table.scrollTo(index)
        row=win.model.rows[win.proxy.mapToSource(index).row()]
        assert row['E']==sku
        for key,value in case['fields'].items():
            actual=sem.order_quantity(row) if key=='qty' else row[key]
            assert isclose(actual,value,rel_tol=1e-12,abs_tol=1e-12),(sku,key,actual,value)
        QTest.mouseClick(win.table.viewport(),Qt.MouseButton.LeftButton,pos=win.table.visualRect(index).center())
        if sku not in win.details.toPlainText() or not win.detail_panel.isVisible():win.show_detail(index)
        for label in ('详情','技术依据'):
            if label=='技术依据':win.show_technical_detail(row)
            note=win.details.toPlainText()
            for text in (sku,plan['model_version'],plan['mean_basis'],plan['safety_formula'],*case['reason']):
                assert text in note,(sku,label,text,note)
            state['shown'][sku+' / '+label]=note
            save_picture(win,sku+'-'+label)

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
            assert result['version']['model_version']==plan['model_version']
            assert result['catalogs']['山东仓']['revision']==plan['catalog_revision']
            assert result['effective_settings']['budget'] is None
            state['shown'].update(catalog_revision=plan['catalog_revision'],budget='不限预算',input=plan['sales'])
            report_rows(rows);checked('通过计算按钮取得全店结果，留存全部22323行')
            win.open_overview(win.overview_codes.index(store),0)
            win.mode.setCurrentText('全部商品')
            for case in plan['products']:
                show_live_product(case)
                checked(case['sku']+' 数量及界面计算依据一致')
            state['step']='export';export_button(win.export_all,root/'单店全量明细.xlsx')
        elif state['step']=='export' and not win.worker and (root/'单店全量明细.xlsx').is_file():
            checked('单店全量明细通过界面导出')
            finish()

    def form_values(dialog):
        return {r[0]:r[1:] for r in dialog.model.rows}

    def check_form(case, values):
        expected=(plan['model_version'],plan['mean_basis'],plan['safety_formula'])
        assert tuple(values[key][0] for key in ('模型版本','泊松均值依据','安全库存公式'))==expected,values
        q,net=values['泊松分位数 Q'][0],values['净需求']
        assert q==case['quantile'] and net[0]==case['net'],values
        assert values['安全库存量'][0]==case['safety'],values
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
