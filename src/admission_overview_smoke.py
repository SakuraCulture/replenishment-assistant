"""Native source/frozen acceptance. Explicitly isolated synthetic records only."""
import copy,json,os,sys,time,traceback
from pathlib import Path


def start_smoke(app,win,args):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication,QMessageBox,QPushButton
    from admission_overview_ui import Overview
    from admission_ui import Editor
    from import_ui import ImportDialog
    from unittest.mock import patch
    out=Path(args[0]).resolve()
    if not (out/'synthetic.json').is_file() or Path(os.environ.get('REPLENISHMENT_DATA_DIR','')).resolve()!=out/'settings':raise ValueError('需要隔离合成资料')
    def execute():
        from datetime import date,timedelta
        import admission,import_templates
        from version_info import diagnostic
        from openpyxl import load_workbook
        from parameter_workspace_smoke import fixture
        report={'passed':False,'frozen_exe':bool(getattr(sys,'frozen',False)),'checks':[],**diagnostic()};errors=[];dialogs=[];beats=[]
        oldhook=sys.excepthook;sys.excepthook=lambda *e:errors.append(''.join(traceback.format_exception(*e)))
        heartbeat=QTimer();heartbeat.setInterval(25);heartbeat.timeout.connect(lambda:beats.append(time.monotonic()));heartbeat.start()
        def require(ok,text):
            if not ok:raise AssertionError(text)
            report['checks'].append(text)
        def click(d,text):next(b for b in d.findChildren(QPushButton) if b.text()==text or b.accessibleName()==text).click()
        def drive(callback,handler):
            seen=[];timer=QTimer();timer.setInterval(25);started=time.monotonic()
            def advance():
                d=QApplication.activeModalWidget()
                if d is None:return
                if time.monotonic()-started>30:errors.append('弹窗超时：'+d.windowTitle());d.reject();return
                if d in seen:return
                seen.append(d)
                def act():
                    try:
                        if isinstance(d,QMessageBox):errors.append(d.text());d.accept()
                        else:handler(d)
                    except Exception:errors.append(traceback.format_exc());d.reject()
                QTimer.singleShot(0,act)
            timer.timeout.connect(advance);timer.start()
            try:callback()
            finally:timer.stop()
            if errors:raise AssertionError(errors)
        try:
            win.auto_check.stop();win.jst_generator.timer.stop();win.jst_generator.stopped=True;win.aoxiang_workflows.timer.stop();win.aoxiang_workflows.stopping=True
            win.loaded_ok(fixture());win.show_section(4);app.processEvents();win.parameter_scene.grab().save(str(out/'01-直接入口.png'))
            day=admission.today();yesterday=(date.fromisoformat(day)-timedelta(days=1)).isoformat()
            qa={'state':'已核验','reviewer':'测试业务','verified_on':yesterday,'expires':day,'source':{'original':{'门店名称':'已删参数仍保留的甲店'}}}
            vals={'A':{'qualification':qa,'refusals':{'001':{'name':'饼干','reason':'本店滞销','active':True},'002':{'name':'汽水','reason':'临时拒收','active':True,'expires':yesterday},'003':{'name':'饮料','reason':'已解除','active':False}}},
                  'ORPHAN':{'qualification':{'state':'无资质','source':{'original':{'门店名称':'仅准入记录门店'}}}},
                  'EXPIRED':{'qualification':{**qa,'expires':yesterday}}}
            admission.save(win.work_repository.path,vals,{s:{} for s in vals});before={s:admission.snapshot(win.work_repository.path,s)['record'] for s in vals}
            def entry(d):
                if isinstance(d,Overview):
                    require(len(d.filtered)==4,'独立准入门店和本次门店均可查询');d.grab().save(str(out/'02-资质现状.png'));d.reject()
            drive(lambda:click(win.parameter_scene,'二类医疗资质'),entry)
            q=Overview(win,win);dialogs.append(q);q.reload();q.show();app.processEvents();q.state.setCurrentText('已到期');require(len(q.filtered)==1,'资质按到期状态筛选');q.state.setCurrentText('无资质');require(q.filtered[0][0]=='ORPHAN','无参数记录的门店资质仍可定位')
            def details(d):
                if isinstance(d,Editor):require(d.readonly and d.qfields['note'].isReadOnly(),'详情只读');d.grab().save(str(out/'03-资质详情.png'));click(d,'关闭')
            drive(q.details,details)
            def cancel(d):
                if isinstance(d,Editor):d.qfields['note'].setText('取消测试');click(d,'取消，不保存')
            drive(q.edit,cancel);require({s:admission.snapshot(win.work_repository.path,s)['record'] for s in vals}==before,'查询、详情和取消维护不修改准入记录')
            q.hide();r=Overview(win,win,'refusals');dialogs.append(r);r.reload();r.show();app.processEvents();require(len(r.filtered)==1 and r.filtered[0][2]=='001','拒收清单默认只显示有效拒收');r.grab().save(str(out/'04-拒收现状.png'))
            def release(d):
                if isinstance(d,Editor):click(d,'解除选中商品拒收');click(d,'核对并保存本店准入')
                elif d.objectName()=='interactionReview':d.grab().save(str(out/'05-解除核对.png'));d.accept()
            drive(r.edit,release);saved=admission.snapshot(win.work_repository.path,'A')['record'];require(not saved['refusals']['001']['active'],'解除经确认保存并刷新');require(saved['qualification']==qa,'拒收维护保留原资质及来源');require(not r.filtered,'解除后不再显示在默认有效列表');r.state.setCurrentText('已解除');require(len(r.filtered)==2,'可查已解除记录')
            for kind,dialog,raw in [('store_qualification',q,{'门店编码':'NEW','门店名称':'导入新店','资质状态':'已核验','核验人':'测试员','核验日期':day}),('store_refusals',r,{'门店编码':'NEW','门店名称':'导入新店','商品编码':'000099','商品名称':'新品饼干','拒收原因':'不经营','到期日':yesterday})]:
                path=out/(kind+'.xlsx');import_templates.write(kind,path);wb=load_workbook(path);ws=wb[import_templates.TEMPLATES[kind][1]]
                for cell in ws[1]:
                    if cell.value in raw:ws.cell(2,cell.column,raw[cell.value])
                wb.save(path);wb.close();dialog.show()
                def imported(d):
                    if isinstance(d,ImportDialog):require(d.acceptDrops(),'复用拖拽导入');d.set_paths([path]);d.go.click()
                    elif d.objectName()=='interactionReview':d.accept()
                drive(dialog.import_file,imported);require(len(dialog.filtered)==1 and dialog.filtered[0][0]=='NEW','导入后只定位本次涉及记录：'+kind);require(dialog.state.currentText()=='全部','已到期导入记录也可立即看到：'+kind);dialog.grab().save(str(out/('06-'+kind+'.png')))
                export=out/(kind+'-查询.xlsx')
                with patch('export_locations.save_file',return_value=(str(export),'Excel')):dialog.export_filtered()
                wb=load_workbook(export);require(wb.worksheets[0].max_row==2 and wb.worksheets[0]['A2'].value=='NEW','实际导出仅包含当前筛选：'+kind);wb.close();dialog.show_all();require(len(dialog.filtered)>1,'清除导入范围可回到全部：'+kind);dialog.hide()
            require(admission.snapshot(win.work_repository.path,'ORPHAN')['record']==before['ORPHAN'],'未选门店准入不变')
            require(win.work_repository.stores()=={},'查看和准入维护不复活已删除门店参数配置')
            require(len(beats)>0,'后台查询和保存期间Qt事件循环可运行');require(not errors,'无未捕获Qt异常');report['passed']=True
        except Exception:report['error']=traceback.format_exc();report['gui_errors']=errors
        finally:
            heartbeat.stop()
            for d in dialogs:d.close()
            sys.excepthook=oldhook;(out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str),'utf-8');win._discard_close=True;app.exit(0 if report['passed'] else 1)
    QTimer.singleShot(300,execute)
