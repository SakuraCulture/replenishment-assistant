"""Actual Qt/frozen acceptance. Synthetic data and an explicit isolated directory only."""
import copy,json,os,sys,time,traceback
from pathlib import Path
from PySide6.QtCore import QTimer,Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication,QMessageBox,QPushButton,QDialog


def fixture():
    import engine
    rows=[]
    for s in ('A','B'):
        d=dict(A=s,B=s,D='测试商品',E='1001',H=2,I=14,J=60,K=3,L=0,N=0,O=1,V=8,S='启用',AU='山东仓',_source='资料匹配')
        m=dict(export_row=2,product_row=2,match='商品编码精确匹配',blocking=[],notes=[],category='休闲食品',export_status='正常',export_updated=None,product_tags='',product_tags_known=True,medical_sources=[])
        rows.append((d,m))
    return dict(normalized=rows,config=engine.config(),stores={'A':'测试A','B':'测试B'},blocked={},hashes={},intake={'files':[],'conflicts':[],'duplicates':0,'products':''})


def start_smoke(app,win,args):
    out=Path(args[0]).resolve()
    if not (out/'synthetic.json').is_file() or Path(os.environ.get('REPLENISHMENT_DATA_DIR','')).resolve()!=out/'settings':raise ValueError('Isolated synthetic settings required')
    def run():
        import parameter_schema as schema,parameter_maintenance as pm,store_parameter_import as spi,store_parameter_ui as sui
        from parameter_maintenance_ui import PatchDialog,MappingDialog,edit_stores
        from import_ui import ImportDialog
        import parameter_tables,import_templates,replenishment_plans as plans
        from replenishment_plan_ui import Library,Editor
        from work_repository import now,today
        report={'passed':False,'frozen_exe':bool(getattr(sys,'frozen',False)),'checks':[]};checks=report['checks'];beats=[]
        timer=QTimer();timer.setInterval(25);timer.timeout.connect(lambda:beats.append(time.monotonic()));timer.start()
        dialogs=[]
        def require(ok,message):
            if not ok:raise AssertionError(message)
        def wait(pred,timeout=10):
            end=time.monotonic()+timeout
            while not pred() and time.monotonic()<end:app.processEvents();QTest.qWait(15)
            require(pred(),'UI timeout')
        def click(widget,text):
            b=next((x for x in widget.findChildren(QPushButton) if x.text()==text),None)
            require(b is not None,'Missing button '+text);b.click()
        def drive(callback,changes=None,scope=None,cancel=False,paths=None):
            seen=set();errors=[];ticks=QTimer();ticks.setInterval(20);start=time.monotonic()
            def advance():
                d=QApplication.activeModalWidget()
                if not d:return
                if time.monotonic()-start>20:
                    errors.append('Dialog timeout '+d.windowTitle());d.reject();return
                if id(d) in seen:return
                seen.add(id(d))
                try:
                    if isinstance(d,PatchDialog):
                        d.clear()
                        for f,v in (changes or {}).items():
                            ch,mode,edit=d.controls[f];ch.setChecked(True)
                            if v=='default':mode.setCurrentIndex(1)
                            else:mode.setCurrentIndex(0);edit.setText(str(v))
                        d.grab().save(str(out/'02-勾选字段.png'));click(d,'核对修改')
                    elif isinstance(d,sui.Preview):
                        if scope:d.scope.setCurrentIndex(d.scope.findData(scope))
                        require(not d.prepared['errors'],str(d.prepared['errors']))
                        if cancel:d.reject()
                        else:require(d.go.isEnabled(),'Preview disabled');d.go.click()
                    elif isinstance(d,ImportDialog):d.set_paths(paths);d.go.click()
                    elif isinstance(d,QMessageBox):
                        if d.icon()==QMessageBox.Icon.Warning:errors.append(d.text())
                        d.accept()
                    elif d.objectName()=='interactionReview':
                        if cancel:d.reject()
                        else:d.accept()
                except Exception:errors.append(traceback.format_exc());d.reject()
            ticks.timeout.connect(advance);ticks.start()
            try:callback()
            finally:ticks.stop()
            require(not errors,'\n'.join(errors))
        try:
            build=Path(getattr(sys,'_MEIPASS',Path(__file__).parent))/'build_info.json'
            report['build_commit']=json.loads(build.read_text('utf-8'))['source_commit'] if build.exists() else 'source'
            a,b=spi.base(),spi.base()
            for r in (a,b):r['ui'].update(primary='山东仓',secondary=None,mode=1)
            a['preset']['config']['parameters']['probability']=.95;b['preset']['config']['parameters']['probability']=.8
            b['preset']['config']['warehouses']['山东仓']=[4,2]
            win.work_repository.save_stores({'A':a,'B':b});win.loaded_ok(fixture());win.show_section(4);app.processEvents()
            scene=win.parameter_scene;scene.check_visible(True);scene.grab().save(str(out/'01-参数入口.png'))
            old=copy.deepcopy(win.work_repository.stores())
            drive(lambda:scene.edit_defaults(),{('warehouses','河北仓',0):6},'both',cancel=True)
            require(win.work_repository.stores()==old,'Cancel changed defaults');checks.append('批量设置预览取消，未改配置')
            drive(lambda:scene.edit_defaults(),{('warehouses','河北仓',0):6},'both')
            for s,p in [('A',.95),('B',.8)]:
                require(win.config_for(s)['parameters']['probability']==p,'Cross-store common changed')
                require(win.config_for(s)['warehouses']['河北仓'][0]==6,'Current not applied')
                require(win.work_repository.stores()[s]['preset']['config']['warehouses']['河北仓'][0]==6,'Default not saved')
            checks.append('实际勾选两店，只改河北备货天数，同时保存默认和应用本次；各店差异保留')
            drive(lambda:edit_stores(win,win,['A'],'common','current'),{('parameters','probability'):'default'},'current')
            require(win.config_for('A')['parameters']['probability']==.9 and win.config_for('B')['parameters']['probability']==.8,'Common scope wrong')
            checks.append('单店通用参数独立恢复默认，其他门店和保存默认不变')
            p=out/'完整门店参数.xlsx';parameter_tables.write(p,'classified_parameters',{'A':a,'B':b},scoped=True)
            from openpyxl import load_workbook
            wb=load_workbook(p);wb['分仓周期']['D2']=7;wb.save(p);wb.close()
            drive(lambda:sui.import_parameters(scene,win,kind='classified_parameters'),scope='defaults',paths=[str(p)])
            require(win.work_repository.stores()['A']['preset']['config']['warehouses']['河北仓'][0]==7,'Excel not imported')
            checks.append('新完整模板实际读取、差异预览、保存默认；当前参数不被隐式改写')
            mapping=MappingDialog(win,win);dialogs.append(mapping);mapping.show()
            c=copy.deepcopy(win.config_for('A'));mapping.fill({'休闲食品':'日百'})
            drive(mapping.save);require(win.config_for('A')==c,'Saving global changed current')
            drive(mapping.apply_current);mapping.grab().save(str(out/'03-全局映射.png'))
            require(all(win.config_for(s)['category_profiles']=={'休闲食品':'日百'} for s in ('A','B')),'Mapping not shared')
            checks.append('全局映射独立保存、明确应用本次；两店共用映射，分类权重仍独立')
            mapping.close()
            editor=Editor(win);dialogs.append(editor);editor.name.setText('测试预案')
            for i in range(editor.stores.count()):editor.stores.item(i).setCheckState(Qt.CheckState.Checked)
            drive(lambda:editor.edit(),{('warehouses','山东仓',0):5})
            editor.save();require(editor.result()==QDialog.DialogCode.Accepted,'Plan not saved')
            require(editor.saved['parameter_fields']==[['warehouses','山东仓',0]],'Plan stores unintended fields')
            library=Library(win,True);dialogs.append(library);drive(library.adopt)
            require(win.config_for('A')['warehouses']['山东仓'][0]==5,'Plan not applied')
            require(win.config_for('B')['parameters']['probability']==.8,'Plan overrode unrelated common value')
            checks.append('预案保存、重新读取并采用，仅应用勾选字段')
            frozen=copy.deepcopy(win.snapshot());before=copy.deepcopy(win.config_for('A'))
            snap=pm.mapping_snapshot(win.work_repository.path);pm.save_mapping(win.work_repository.path,{'休闲食品':'快销'},snap)
            win.restore_job({'day':today(),'saved':now(),'payload':frozen});require(win.config_for('A')==before,'Restored snapshot changed')
            win.reset_job();win.loaded_ok(fixture());require(win.config_for('A')['category_profiles']=={'休闲食品':'快销'},'New job missed global mapping')
            require(win.config_for('A')['warehouses']['河北仓'][0]==7,'New job missed imported default')
            checks.append('恢复旧作业保留快照；新建重新检查带入新映射和已保存门店默认')
            for kind in ('classified_parameters','common_parameters','global_category_mapping'):import_templates.write(kind,out/(kind+'.xlsx'))
            win.concurrency.setValue(2);win.start_calculation(['A','B'])
            require(win.worker is not None,'Calculation did not start')
            wait(lambda:win.worker is None,45)
            require(all(win.states[s]['state']=='已完成' for s in ('A','B')),str(win.states))
            r=win.results['A'];require(bool(r['rows']),'Calculation empty');report['calculation_summary']=r['summary']
            checks.append('实际双店后台计算及 Windows spawn 使用组装后的门店参数，全部完成')
            report['passed']=True
        except Exception:report['error']=traceback.format_exc()
        finally:
            for d in dialogs:d.close();d.deleteLater()
            win.job_dirty=False;win._discard_close=True;win.close()
            try:wait(lambda:win.aoxiang_workflows.session.client is None and win.aoxiang_workflows.session_closer is None and win.aoxiang_workflows.owner is None)
            except Exception:report['passed']=False;report['shutdown_error']=traceback.format_exc()
            timer.stop();report['heartbeat_ticks']=len(beats);report['max_heartbeat_gap_seconds']=max((b-a for a,b in zip(beats,beats[1:])),default=0)
            (out/'ui-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            QTimer.singleShot(100,lambda:app.exit(0 if report['passed'] else 1))
    QTimer.singleShot(250,run)
