"""Native Qt acceptance for source and frozen app; isolated synthetic data only."""
import copy,json,os,sys,time,traceback
from pathlib import Path


def start_smoke(app,win,args):
    from PySide6.QtCore import QTimer,Qt
    from PySide6.QtWidgets import QApplication,QDialog,QMessageBox,QPushButton,QComboBox,QCheckBox
    from parameter_quick_ui import QuickSettings,open_quick
    from parameter_preset_ui import choose_preset
    from store_parameter_ui import Preview
    out=Path(args[0]).resolve()
    if not (out/'synthetic.json').is_file() or Path(os.environ.get('REPLENISHMENT_DATA_DIR','')).resolve()!=out/'settings':raise ValueError('需要独立合成验收目录')
    def execute():
        import parameter_presets as pp,parameter_schema as schema,store_parameter_import as spi
        import import_templates,store_parameter_ui as ui
        from openpyxl import load_workbook
        from parameter_workspace_smoke import fixture
        from version_info import diagnostic
        report={'passed':False,'frozen_exe':bool(getattr(sys,'frozen',False)),'checks':[],**diagnostic()};errors=[]
        old_hook=sys.excepthook;sys.excepthook=lambda *exc:errors.append(''.join(traceback.format_exception(*exc)))
        def require(ok,text):
            if not ok:raise AssertionError(text)
            report['checks'].append(text)
        def click(d,text):next(b for b in d.findChildren(QPushButton) if b.text()==text).click()
        def drive(callback,*,cancel=False,preset=False,save=False):
            seen=set();timer=QTimer();timer.setInterval(25);start=time.monotonic()
            def advance():
                d=QApplication.activeModalWidget()
                if not d:return
                if time.monotonic()-start>25:errors.append('超时：'+d.windowTitle());d.reject();return
                if id(d) in seen:return
                seen.add(id(d))
                try:
                    if isinstance(d,QuickSettings):
                        d.fields['新品铺设'].setCurrentIndex(2);d.budget.setCurrentIndex(2)
                        click(d,'核对变化');require(d.isVisible() and '需要填写' in d.error.text(),'限定预算空金额在表单内提示，不关闭窗口')
                        d.amount.setText('0');d.grab().save(str(out/'02-批量选项.png'));click(d,'核对变化')
                    elif isinstance(d,Preview):
                        require(not d.scope.isEnabled(),'批量设置作用范围固定，不在最后换范围')
                        d.grab().save(str(out/'03-逐店核对.png'))
                        if cancel:click(d,'取消，不修改')
                        else:d.go.click()
                    elif d.objectName()=='applyParameterPreset':
                        box=d.findChild(QComboBox);box.setCurrentIndex(box.findData(p['id']))
                        check=d.findChild(QCheckBox);require(not check.isChecked(),'保存以后默认默认不勾选')
                        check.setChecked(save);d.grab().save(str(out/'04-方案摘要.png'));click(d,'核对差异')
                    elif d.objectName()=='interactionReview':
                        d.grab().save(str(out/'05-方案差异.png'));d.reject() if cancel else d.accept()
                    elif isinstance(d,QMessageBox):errors.append(d.text());d.accept()
                except Exception:errors.append(traceback.format_exc());d.reject()
            timer.timeout.connect(advance);timer.start()
            try:callback()
            finally:timer.stop()
            if errors:raise AssertionError(errors)
        try:
            win.auto_check.stop();win.jst_generator.timer.stop();win.jst_generator.stopped=True
            win.aoxiang_workflows.timer.stop();win.aoxiang_workflows.stopping=True
            a,b=spi.base(),spi.base()
            for r in (a,b):r['ui'].update(primary='山东仓',mode=1)
            a['preset']['config']['warehouses']['山东仓']=[6,3];b['preset']['config']['warehouses']['山东仓']=[4,2]
            win.work_repository.save_stores({'A':a,'B':b});win.loaded_ok(fixture());win.show_section(4);app.processEvents()
            scene=win.parameter_scene;scene.refresh();scene.check_visible(True);scene.grab().save(str(out/'01-快捷入口.png'))
            before=win.work_repository.stores();current=copy.deepcopy(win.config_for('A'));drive(scene.quick_settings,cancel=True)
            require(win.work_repository.stores()==before,'取消批量核对不写任何默认')
            drive(scene.quick_settings)
            saved=win.work_repository.stores();require(all(saved[s]['ui']['amount']=='0' for s in ('A','B')),'批量预算0保存成功')
            require(saved['A']['preset']['config']['warehouses']['山东仓']==[6,3] and saved['B']['preset']['config']['warehouses']['山东仓']==[4,2],'批量开关预算不覆盖各店不同周期')
            require(win.config_for('A')==current and win.store_ui('A')['mode']==1,'保存未来默认不改变当前作业')
            c=schema.standard();c['warehouses']['山东仓']=[7,2];p=pp.save(win.work_repository,'测试七天方案',c)
            drive(lambda:choose_preset(win,win,['A'],'current'),cancel=True,preset=True)
            require(win.config_for('A')==current,'取消方案核对不改变本次')
            drive(lambda:choose_preset(win,win,['A'],'current'),preset=True)
            require(win.config_for('A')['warehouses']['山东仓']==[7,2] and win.config_for('B')['warehouses']['山东仓']==[4,2],'方案仅应用所选门店本次，未串店')
            require(win.work_repository.stores()==saved and win.store_ui('A')['mode']==1,'应用本次不改默认、预算和仓')
            drive(lambda:choose_preset(win,win,['B'],'current'),preset=True,save=True)
            require(win.work_repository.stores()['B']['preset']['id']==p['id'] and win.config_for('B')['warehouses']['山东仓']==[7,2],'显式勾选可同时保存以后默认')
            # Produce the actual app template, fill row 2 and use its parser/preview/commit.
            path=out/'门店预算模板.xlsx';import_templates.write('store_budget',path)
            wb=load_workbook(path);ws=wb['门店预算'];require(any('不限预算' in (dv.formula1 or '') for dv in ws.data_validations.dataValidation),'应用生成的预算模板含下拉选项')
            require('条件必填' in ws['D1'].comment.text,'预算金额含条件必填提示')
            for col,value in enumerate(['A','','限定预算',3000],1):ws.cell(2,col,value)
            wb.save(path);wb.close();data=spi.read([path]);snapshot=ui.capture(win,data);prepared=ui.preview(data,snapshot,'defaults');require(not prepared['errors'],'预算模板填写后解析校验通过')
            ui.commit(win,data,snapshot,'defaults',prepared);require(win.work_repository.stores()['A']['ui']['amount']=='3000','模板导入与界面共用提交，保存金额3000')
            win.show_section(4);scene.refresh();scene.grab().save(str(out/'06-保存后.png'))
            from work_repository import today
            snap=win.snapshot();win.work_repository.save_job(today(),snap);stored=win.work_repository.get_job(today());win.reset_job();win.restore_job(stored)
            require(win.config_for('A')['warehouses']['山东仓']==[7,2],'草稿恢复保留本次采用方案')
            require(not errors,'本次Qt流程无未捕获异常');report['passed']=True
        except Exception:report['error']=traceback.format_exc();report['gui_errors']=errors
        finally:
            sys.excepthook=old_hook;(out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str),'utf-8');win._discard_close=True;app.exit(0 if report['passed'] else 1)
    QTimer.singleShot(300,execute)
