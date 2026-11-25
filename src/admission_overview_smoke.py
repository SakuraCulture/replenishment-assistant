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
                wb=load_workbook(export);require(wb.worksheets[0].max_row==2 and wb.worksheets[0]['A2'].value=='NEW','实际导出仅包含当前筛选：'+kind)
                if kind=='store_qualification':
                    _,sheet,required,optional=import_templates.TEMPLATES[kind]
                    require(wb.active.title==sheet and [c.value for c in wb.active[1]]==required+optional,'资质导出与导入模板的表名和表头一致')
                    wb.active.cell(2,(required+optional).index('备注')+1,'合成维护表回导测试')
                    wb.save(export);wb.close();path=export
                    drive(dialog.import_file,imported)
                    adopted=admission.snapshot(win.work_repository.path,'NEW')['record']['qualification']
                    require(adopted['note']=='合成维护表回导测试' and adopted['state']=='已核验','从同一入口回导已编辑的资质维护表')
                wb.close();dialog.show_all();require(len(dialog.filtered)>1,'清除导入范围可回到全部：'+kind);dialog.hide()
            require(admission.snapshot(win.work_repository.path,'ORPHAN')['record']==before['ORPHAN'],'未选门店准入不变')
            require(win.work_repository.stores()=={},'查看和准入维护不复活已删除门店参数配置')
            verify_release_import(win,out,drive,require,click)
            if '--bulk' in args:
                from bulk_refusal_smoke import verify
                verify(app,win,out,report,drive,require)
            require(len(beats)>0,'后台查询和保存期间Qt事件循环可运行');require(not errors,'无未捕获Qt异常');report['passed']=True
        except Exception:report['error']=traceback.format_exc();report['gui_errors']=errors
        finally:
            heartbeat.stop()
            for d in dialogs:d.close()
            sys.excepthook=oldhook;(out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str),'utf-8');win._discard_close=True;app.exit(0 if report['passed'] else 1)
    QTimer.singleShot(300,execute)


def verify_release_import(win, out, drive, require, click):
    from contextlib import closing
    from unittest.mock import patch
    from openpyxl import load_workbook
    from PySide6.QtWidgets import QTableView
    from admission_overview_ui import Overview
    from import_ui import ImportDialog
    import admission

    path = win.work_repository.path
    source = out/'批量解除拒收模板.xlsx'
    long_code = '@0001234567890123456789'
    original = {
        'REL-A': {'refusals': {long_code: {'name': '合成商品', 'reason': '合成拒收', 'active': True,
                                          'source': {'original': {'门店名称': '甲店'}}},
                              '000003': {'reason': '先前已解除', 'active': False}}},
        'REL-B': {'refusals': {long_code: {'reason': '另一门店继续拒收', 'active': True},
                              '000002': {'reason': '本次解除', 'active': True}}},
    }
    admission.save(path, original, {s: {} for s in original})
    expected = copy.deepcopy(original)
    expected['REL-A']['refusals'][long_code]['active'] = False
    expected['REL-B']['refusals']['000002']['active'] = False

    def exercise(action):
        def handle(d):
            if isinstance(d, Overview):
                d.search_fields[0].setText('测试筛选无结果')
                d.filter()
                require(not d.filtered and d.release_import_btn.isEnabled(), '导入批量解除可在无勾选和空筛选时进入')
                click(d, '导入批量解除')
                if action == 'confirm':
                    require(d.state.currentText() == '已解除' and {(r[0], r[2]) for r in d.filtered} ==
                            {('REL-A', long_code), ('REL-B', '000002')}, '确认后定位两条实际解除记录')
                    d.grab().save(str(out/'14-导入批量解除结果.png'))
                d.reject()
            elif isinstance(d, ImportDialog):
                if not source.exists():
                    with patch('export_locations.save_file',return_value=(str(source),'Excel')), \
                            patch('import_ui.QMessageBox.information'):
                        click(d, '下载批量解除拒收模板')
                    with closing(load_workbook(source)) as book:
                        ws = book.active
                        require([c.value for c in ws[1]] == ['门店编码', '商品编码'] and ws['B2'].number_format == '@',
                                '从导入窗口下载的模板含两个文本编码列')
                        entries = [('REL-A', long_code), ('REL-B', '000002'), ('REL-A', long_code),
                                   ('REL-A', '000003'), ('UNKNOWN', long_code), ('REL-B', None)]
                        for i, (store, sku) in enumerate(entries, 2):
                            ws.cell(i, 1, store)
                            ws.cell(i, 2, sku)
                        book.save(source)
                with patch('import_ui.QFileDialog.getOpenFileName',return_value=(str(source),'Excel')):
                    click(d, '选择文件')
                click(d, '读取并校验')
            elif d.objectName() == 'interactionReview':
                preview = d.findChild(QTableView).model().rows
                require([r[3] for r in preview] == ['将解除', '将解除', '跳过 · 表内重复', '跳过 · 已解除',
                                                   '跳过 · 未找到拒收记录', '跳过 · 编码无效（商品编码）'],
                        '导入预览按原文件行序列出应解除和跳过原因')
                if action == 'confirm':
                    d.grab().save(str(out/'13-导入批量解除预览.png'))
                click(d, '取消' if action == 'cancel' else '确认批量解除')
            elif d.windowTitle() == '没有可解除的拒收':
                require(action == 'replay' and d.findChild(QTableView).model().rowCount() == 6,
                        '重复导入只显示核对结果，不能再次保存')
                click(d, '关闭')
        drive(lambda: click(win.parameter_scene, '门店拒收清单'), handle)

    before = path.read_bytes()
    exercise('cancel')
    require(path.read_bytes() == before, '取消导入预览未写入拒收库')
    exercise('confirm')
    require({s: admission.snapshot(path,s)['record'] for s in original} == expected,
            '逐店逐SKU对账仅解除两条，保留另店同码拒收及原资料')
    after = path.read_bytes()
    exercise('replay')
    require(path.read_bytes() == after, '重复导入未登记额外变更')
