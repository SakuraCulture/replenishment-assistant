"""Frozen UI and BIFF acceptance, only in an explicitly synthetic directory."""
from contextlib import closing
import json
import sys
import time
import traceback
from pathlib import Path

from PySide6.QtCore import QTimer,Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QMessageBox, QPushButton


def start_smoke(app,win,args):
    out=Path(args[0]).resolve()
    reopen='reopen' in args[1:]
    win.auto_check.stop()
    win.jst_generator.stopped=True
    win.jst_generator.timer.stop()
    win.aoxiang_workflows.stopping=True
    win.aoxiang_workflows.timer.stop()

    def check(value,message):
        if not value:
            raise AssertionError(message)

    def wait(predicate):
        end=time.monotonic()+25
        while not predicate() and time.monotonic()<end:
            QTest.qWait(20)
        check(predicate(),'隔离界面未完成')

    def run():
        import openpyxl
        import xlrd
        from jst_auto_order import export_file
        from jst_auto_order_store import list_orders, profiles
        from jst_auto_order_ui import DeliveryReview, Orders, Profiles
        from jst_delivery_profiles_xlsx import HEADERS, SHEET
        from lifecycle_home_ui import StageDialog
        from version_info import diagnostic
        from work_repository import read_connection
        diagnostics=[]
        result={}
        stage=None
        reuse=None
        timer=QTimer(win)
        timer.setInterval(30)
        review_timer=QTimer(win)
        review_timer.setInterval(30)
        detail={}

        def choose_review():
            window=app.activeModalWidget()
            if not isinstance(window,DeliveryReview):
                return
            review_timer.stop()
            choice=detail.pop('review_choice')
            next(b for b in window.findChildren(QPushButton) if b.text()==choice).click()

        review_timer.timeout.connect(choose_review)

        def tick():
            window=app.activeModalWidget()
            try:
                if isinstance(window,Profiles) and not detail.get('saved'):
                    detail['saved']=True
                    window.scope.setCurrentIndex(window.keys.index((h['store'],h['warehouse'])))
                    values={'buyer':'000TEST','recipient':'合成收货人','phone':'00000000000',
                            'province':'河北省','city':'石家庄市','district':'桥西区','address':'合成测试路1号'}
                    for key,value in values.items():window.fields[key].setText(value)
                    next(b for b in window.findChildren(QPushButton) if b.text()=='保存本仓收货资料').click()
                    check('已保存' in window.status.text(),'资料维护未保存')
                    check(window.export_path(out/'合成已保存配送资料.xlsx'),'两仓配送资料表未导出')
                    template_btn=next(b for b in window.findChildren(QPushButton) if b.text()=='下载导入模板')
                    check(template_btn.isEnabled(),'下载导入模板入口不可用')
                    sheet_file=out/'合成配送资料模板.xlsx'
                    check(window.template_path(sheet_file),'配送资料导入模板未下载')
                    with closing(openpyxl.load_workbook(sheet_file)) as book:
                        sheet=book[SHEET]
                        check(tuple(cell.value for cell in sheet[1])==HEADERS[:-1],'空白模板表头不符')
                        check(not any(cell.value is not None for row in sheet.iter_rows(min_row=2) for cell in row),
                              '空白模板含已保存资料')
                        warehouse_col=HEADERS.index('供货仓')+1
                        address_col=HEADERS.index('地址')+1
                        other='山东仓' if h['warehouse']=='河北仓' else '河北仓'
                        for row,warehouse,address in ((2,h['warehouse'],'合成批量修改路2号'),(3,other,'合成另一仓地址')):
                            sheet.cell(row,1).value=h['store']
                            sheet.cell(row,warehouse_col).value=warehouse
                            sheet.cell(row,address_col).value=address
                        book.save(sheet_file)
                    detail['template_downloaded']=True
                    detail['review_choice']='取消'
                    review_timer.start()
                    check(not window.import_path(sheet_file),'取消预览后仍有写入')
                    with closing(read_connection(path)) as db:
                        initial=profiles(db)
                        check(len(initial)==1 and next(iter(initial.values()))['values']==values,'取消改动了资料')
                    detail['review_choice']='确认批量保存'
                    review_timer.start()
                    check(window.import_path(sheet_file),'预览确认后未保存')
                    check('已批量保存 2 份' in window.status.text(),'两仓保存数量不一致')
                    check(window.fields['address'].text()=='合成批量修改路2号','未显示更新后的地址')
                    check(window.fields['buyer'].text()=='000TEST','批量覆盖未改字段')
                    detail['bulk_saved']=2
                    window.grab().save(str(out/'配送资料批量保存.png'))
                    window.accept()
                elif isinstance(window,Orders):
                    if reuse:
                        tasks={r['warehouse']:[r['online'],r['stage']] for r in window.records}
                        check(tasks==reuse['ready'],'准备结果未打开原仓单任务')
                        hint=window.note.text()
                        check(all(text in hint for text in reuse.get('expected_hints', [])),
                              '部分仓单失败时未提示原因')
                        check(all(not r['internal'] for r in window.records),'制表触发了平台原单关联')
                        detail.update(tasks=tasks,notice=hint)
                        window.grab().save(str(out/'继续原仓单任务.png'))
                        window.accept()
                        return
                    check(window.table.rowCount()==1,'任务结果没有准确展示所选仓单')
                    check(not window.buttons['submit'].isEnabled(),'未经平台预览已能正式提交')
                    r=window.records[0]
                    detail['online']=r['online']
                    check(r['stage']=='PREPARED' and not r['internal'],'离线状态不符')
                    window.grab().save(str(out/('任务重开.png' if reopen else '下单任务.png')))
                    window.accept()
                elif isinstance(window,QMessageBox):
                    diagnostics.append(window.windowTitle()+' '+window.text());window.reject()
            except Exception:
                diagnostics.append(traceback.format_exc())
                window.reject()
        timer.timeout.connect(tick)
        try:
            path=win.work_repository.path.resolve()
            marker=json.loads((out/'synthetic.json').read_text('utf-8'))
            check(path==out/'settings/workbench.sqlite3' and marker.get('synthetic') is True
                  and marker.get('live_platform') is False,'验收要求独立合成资料')
            reuse=marker.get('reuse_existing')
            stage=StageDialog(win.followup,'jst',reuse['heads'] if reuse else None)
            stage.setModal(True);stage.show();panel=stage.panel
            wait(lambda:panel.data is not None)
            timer.start()
            if reuse:
                panel.select_visible()
                check(len(panel.chosen)==2,'合成双仓范围不符')
                panel.auto_prepare_btn.click()
                check(detail.get('tasks')==reuse['ready'],'双仓准备未完成')
                check(len(list_orders(path))==2,'重复准备改变了仓单任务数')
            elif reopen:
                panel.auto_tasks_btn.click()
                check(detail.get('online')==list_orders(path)[0]['online'],'重开丢失原线上单号')
                with closing(read_connection(path)) as db:
                    addresses={record['values']['address'] for record in profiles(db).values()}
                check(addresses=={'合成批量修改路2号','合成另一仓地址'},'重开丢失批量保存的两个仓地址')
                detail['bulk_reopened']=len(addresses)
            else:
                ready=[r for r in panel.records if r['state']=='ready']
                check(len(ready)==1,'合成有效仓单范围不符')
                h=ready[0]['head']
                check(panel.auto_profiles_btn.isEnabled(),'未选择仓单时无法维护配送资料')
                stage.hide()
                win.show_section(4)
                next(b for b in win.parameter_scene.findChildren(QPushButton) if b.text()=='聚水潭配送资料维护').click()
                check(detail.get('bulk_saved')==2,'参数设置未完成批量维护')
                stage.show()
                panel.chosen={(h['oid'],h['warehouse'])}
                panel.render();panel.controls()
                panel.auto_prepare_btn.click()
                check('online' in detail and not diagnostics,'自动下单入口未完成')
                target=out/'合成下单样表.xls'
                export_file(path,detail['online'],target)
                sheet=xlrd.open_workbook(str(target)).sheet_by_name('订单信息')
                check((sheet.nrows,sheet.ncols)==(2,39),'原生 XLS 结构不符')
                check(sheet.cell_value(1,2)=='000TEST' and sheet.cell_value(1,20)=='0001*40'
                      and sheet.cell_value(1,23)==sheet.cell_value(1,24)=='' and sheet.cell_value(1,29)=='现场取货'
                      and sheet.cell_value(1,17)=='合成批量修改路2号',
                      '冻结 EXE 生成的 XLS 字段不符')
                detail.update(file_size=target.stat().st_size,sku_cell=sheet.cell_value(1,20))
            check(not diagnostics,'界面报错')
            result={'passed':True,'facts':detail,**diagnostic()}
        except Exception:
            result={'passed':False,'error':traceback.format_exc(),'messages':diagnostics}
        finally:
            timer.stop()
            review_timer.stop()
            if stage:
                stage.panel.shutdown()
                stage.close()
        result['frozen_exe']=bool(getattr(sys,'frozen',False))
        filename='auto-order-reopen.json' if reopen else 'auto-order-ui.json'
        (out/filename).write_text(json.dumps(result,ensure_ascii=False,indent=2),'utf-8')
        win._discard_close=True
        app.exit(0 if result['passed'] else 1)
    QTimer.singleShot(50,run)
