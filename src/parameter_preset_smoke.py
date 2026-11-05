"""Opt-in real Qt/EXE acceptance, isolated synthetic records only."""
import copy,json,os,sys,time,traceback
from pathlib import Path


def start_smoke(app,win,args):
    from PySide6.QtCore import QTimer,Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication,QDialog,QMessageBox,QPushButton,QLineEdit
    out=Path(args[0]).resolve()
    if not (out/'synthetic.json').is_file() or Path(os.environ.get('REPLENISHMENT_DATA_DIR','')).resolve()!=out/'settings':raise ValueError('需要独立合成验收目录')
    def execute():
        import parameter_presets as pp,replenishment_plans as plans,parameter_schema as schema
        import parameter_maintenance as pm
        from parameter_preset_ui import PresetLibrary,StorePlanEditor,assign_defaults
        from parameter_ui import ParameterDialog
        from store_parameter_import import base
        from parameter_workspace_smoke import fixture
        from work_repository import now,today
        from version_info import diagnostic
        report={'passed':False,'frozen_exe':bool(getattr(sys,'frozen',False)),'checks':[],**diagnostic()};checks=report['checks'];errors=[]
        old_hook=sys.excepthook;sys.excepthook=lambda *exc:errors.append(''.join(traceback.format_exception(*exc)))
        def require(value,message):
            if not value:raise AssertionError(message)
            checks.append(message)
        def click(d,text):
            b=next(x for x in d.findChildren(QPushButton) if x.text()==text);b.click()
        def drive(callback,kind):
            visited=set();timer=QTimer();timer.setInterval(25);started=time.monotonic()
            def advance():
                d=QApplication.activeModalWidget()
                if not d or id(d) in visited:return
                visited.add(id(d))
                try:
                    if isinstance(d,ParameterDialog):
                        edit=d.layout().itemAt(0).widget();edit.setText('测试活动参数')
                        row=next(i for i in range(d.warehouses.rowCount()) if d.warehouses.item(i,0).text()=='山东仓')
                        d.warehouses.cellWidget(row,1).setValue(3);d.warehouses.cellWidget(row,2).setValue(2)
                        click(d,'核对并保存参数预案')
                    elif isinstance(d,QMessageBox):
                        yes=d.button(QMessageBox.StandardButton.Yes)
                        if yes:yes.click()
                        else:errors.append(d.text());d.accept()
                    elif d.windowTitle()=='选择门店默认参数预案':
                        from PySide6.QtWidgets import QComboBox
                        box=d.findChild(QComboBox);box.setCurrentIndex(box.findData(p['id']));click(d,'核对差异')
                    elif d.objectName()=='interactionReview':d.accept()
                    elif d.windowTitle().startswith('删除参数预案'):
                        d.grab().save(str(out/'04-删除引用核对.png'));d.accept()
                except Exception:errors.append(traceback.format_exc());d.reject()
                if time.monotonic()-started>20:errors.append('弹窗超时');d.reject()
            timer.timeout.connect(advance);timer.start()
            try:callback()
            finally:timer.stop()
            if errors:raise AssertionError(errors)
        try:
            win.auto_check.stop();win.jst_generator.timer.stop();win.jst_generator.stopped=True
            win.aoxiang_workflows.timer.stop();win.aoxiang_workflows.stopping=True
            a,b=base(),base()
            for r in (a,b):r['ui'].update(primary='山东仓',mode=1)
            win.work_repository.save_stores({'A':a,'B':b});win.loaded_ok(fixture());win.show_section(4);app.processEvents()
            library=PresetLibrary(win);library.show();app.processEvents();require(not library.values,'系统内置参数未显示为预案');drive(library.create,'edit')
            p=next(p for p in library.values if p['id']!='standard')
            require(p['config']['warehouses']['山东仓']==[3.0,2.0],'实际参数表编辑、保存预案成功')
            library.grab().save(str(out/'01-参数预案.png'));library.close()
            scene=win.parameter_scene;scene.refresh();scene.check_visible(True)
            current=copy.deepcopy(win.config_for('A'));drive(lambda:assign_defaults(scene),'assign')
            require(win.config_for('A')==current,'选择门店默认没有改动当前作业')
            require(win.work_repository.stores()['A']['preset']['id']==p['id'],'批量选择门店默认预案成功')
            scene.grab().save(str(out/'02-门店默认.png'))
            ordinary=pp.save(win.work_repository,'测试常规参数',schema.standard())
            editor=StorePlanEditor(win);editor.show();editor.name.setText('测试逐店预案');editor.check_visible(True)
            index=next(i for i,r in enumerate(editor.rows) if r['code']=='B');box=editor.table.cellWidget(index,2);box.setCurrentIndex(box.findData(ordinary['id']))
            editor.table.setCurrentCell(index,1);editor.move(-1);app.processEvents();editor.grab().save(str(out/'03-逐店补货预案.png'));editor.save()
            plan=editor.saved;require([r['code'] for r in plan['stores']]==['B','A'],'补货预案保存门店顺序')
            plans.apply(win,plan);require(win.config_for('A')['warehouses']['山东仓']==[3.0,2.0],'跟随门店默认正确应用')
            require(pp.signature(win.config_for('B'))==pp.signature(schema.standard()),'同预案另一店使用指定常规参数，未串店')
            snap=win.snapshot();win.work_repository.save_job(today(),snap);saved=win.work_repository.get_job(today())
            import store_defaults
            win.repository.save_budget('A',True,999);win.catalog_repository.save_route('A',{'primary':'河北仓','secondary':'山东仓'})
            scene.refresh();scene.check_visible(False);scene.table.item(scene.codes.index('A'),0).setCheckState(Qt.CheckState.Checked)
            before_defaults=win.work_repository.stores();drive(scene.delete_defaults,'delete-default')
            require('A' not in win.work_repository.stores(),'整条门店配置已删除，参数仓与预算不留占位')
            require('A' not in scene.codes,'已保存配置列表不再显示被删门店')
            require(win.work_repository.stores()['B']==before_defaults['B'],'删除未影响未选门店')
            require(win.config_for('A')['warehouses']['山东仓']==[3.0,2.0],'删除默认不改当前计算准备')
            scene.grab().save(str(out/'05-整条配置已删除.png'))
            scene.scope.setCurrentIndex(1);require('A' in scene.codes,'当前作业独立查看，未被删除');scene.scope.setCurrentIndex(0)
            win.reset_job();win.loaded_ok(fixture())
            require(pp.signature(win.config_for('A'))==pp.signature(schema.standard()),'下一次准备自动填充内置参数')
            require('A' not in win.work_repository.stores(),'准备自动填充不重新写回门店默认')
            u=win.store_ui('A');require(u['mode']==0 and u['amount']=='' and u['primary'] is None and u['secondary'] is None,'旧独立预算与供货缓存未复活被删配置')
            win.restore_job(saved)
            library=PresetLibrary(win);library.show();library.table.selectRow(next(i for i,x in enumerate(library.values) if x['id']==p['id']))
            drive(library.remove,'delete');library.close()
            require(not store_defaults.has_parameters(win.work_repository.stores()['B']),'删除自定义预案可取消门店默认引用')
            scene.refresh();scene.check_visible(True);drive(scene.delete_defaults,'delete-leftover')
            require(not win.work_repository.stores() and not scene.codes,'旧版仅仓与预算残留也可整条清理')
            require(win.config_for('A')['warehouses']['山东仓']==[3.0,2.0],'删除预案不改变当前作业参数')
            win.reset_job();win.restore_job(saved);require(win.config_for('A')['warehouses']['山东仓']==[3.0,2.0],'历史草稿恢复保留旧预案参数快照')
            # Settings/engine use the frozen config, not newly replaced defaults.
            settings=win.selected_settings('A');require(settings['parameter_config']['warehouses']['山东仓']==[3.0,2.0],'恢复后计算装配仍使用历史冻结参数')
            require(not errors,'本流程无未捕获Qt异常');report['passed']=True
        except Exception:report['error']=traceback.format_exc();report['gui_errors']=errors
        finally:
            sys.excepthook=old_hook
            (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str),'utf-8')
            win._discard_close=True;app.exit(0 if report['passed'] else 1)
    QTimer.singleShot(300,execute)
