"""Opt-in actual dialog/selection/budget acceptance with synthetic warehouse data."""
import copy,hashlib,json,os,sqlite3,time,traceback
from pathlib import Path
from contextlib import closing
from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QMessageBox,QApplication
from version_info import diagnostic

def start_smoke(app,win,args):
    out=Path(args[0]).resolve();f=json.loads((out/'fixture.json').read_text('utf-8'))
    if not f.get('synthetic') or Path(os.environ.get('REPLENISHMENT_DATA_DIR','')).resolve()!=out/'settings':raise ValueError('Explicit isolated synthetic fixture required')
    def run():
        checks=[];dialogs=[]
        def require(ok,text):
            if not ok:raise AssertionError(text)
            checks.append(text)
        def dialog(base,current=None,p=None):
            from new_product_ui import NewProductDialog
            d=NewProductDialog(win,base,current or base,p or f['monthly']);d.show();app.processEvents();dialogs.append(d);return d
        try:
            import new_products as np,first_fill,work_repository as wr
            path=win.work_repository.path;require(path.resolve()==out/'settings/workbench.sqlite3','仅使用本轮隔离资料')
            win.auto_check.stop();win.jst_generator.stopped=True;win.jst_generator.timer.stop();win.aoxiang_workflows.stopping=True;win.aoxiang_workflows.timer.stop()
            until=time.monotonic()+15
            while win.jst_generator.worker is not None and time.monotonic()<until:QTest.qWait(20)
            with closing(sqlite3.connect(path)) as db:before='\n'.join(db.iterdump())
            for name,q,warehouse in [('山东单仓',18,'山东仓'),('河北单仓',30,'河北仓'),('山东主河北从',30,'河北仓')]:
                base=f['bases'][name];original=copy.deepcopy(base);d=dialog(base)
                require(d.widgets[0][1].value()==q and d.rows[0]['AU']==warehouse,'归属仓默认首铺数正确：'+name)
                require(not d.widgets[0][0].isChecked(),'读取首铺默认量不会自动选品：'+name)
                d.grab().save(str(out/(name+'.png')));d.widgets[0][0].setChecked(True);d.preview_btn.click()
                require(d.result()==1,'真实预览按钮接受有效首铺数：'+name)
                changed=np.apply(base,d.selections());row=changed['rows'][0]
                require(row['_new_requested']==q and row['AS']==0 and row['_meta']['first_fill']['raw']==q,'候选首铺数及原模型零建议、仓资料原值可追溯：'+name)
                require(base==original,'预览应用函数不回写原计算快照：'+name);d.close()
            r=f['bases']['山东单仓'];manual=np.apply(r,{'0001':12});d=dialog(r,manual)
            require(d.widgets[0][1].value()==12 and d.widgets[0][0].isChecked(),'重开窗口保留已维护数量，不用资料18覆盖人工12');d.close()
            import new_product_focus
            d=dialog(r,p=new_product_focus.empty('2026-11'));require(d.widgets[0][1].value()==6,'往期新品最小单位默认量保持原规则');d.close()
            d=dialog(f['bases']['缺少首铺数']);require(d.widgets[0][1].value()==6 and '默认使用最小出库单位6' in d.model.quantity_sources[0],'缺少字段按用户要求使用本仓最小量6')
            require(not d.widgets[0][0].isChecked(),'最低默认量仍不自动选品');d.grab().save(str(out/'缺少首铺数默认最低量.png'));d.widgets[0][1].setValue(12);d.widgets[0][0].setChecked(True);d.preview_btn.click()
            require(d.result()==1 and d.selections()['0001']==12,'缺失默认量仍可明确人工维护后预览');d.close()
            d=dialog(f['bases']['不合箱规']);require(d.widgets[0][1].value()==36 and d.rows[0]['_meta']['first_fill']['raw']==31,'资料31向上对齐箱规6为36，原值仍可追溯')
            d.grab().save(str(out/'首铺数向上对齐.png'));d.widgets[0][1].setValue(31);d.scope.setCurrentText('全部新品');d.widgets[0][0].setChecked(True);boxes=[];timer=QTimer();timer.setInterval(20)
            def dismiss():
                box=QApplication.activeModalWidget()
                if isinstance(box,QMessageBox):boxes.append(box.detailedText());box.grab().save(str(out/'箱规不符提示.png'));box.accept();timer.stop()
            timer.timeout.connect(dismiss);timer.start();d.preview_btn.click();timer.stop()
            require(d.result()==0 and boxes and '倍数' in boxes[0] and d.widgets[0][1].value()==31,'已有人工数量保留，手工非法倍数仍明确提示')
            d.widgets[0][1].setValue(36);d.preview_btn.click();require(d.result()==1,'维护为合法倍数后可正常预览');d.close()
            floor_results=[]
            if f.get('floor_cases'):
                import engine,normal_exports,result_semantics as sem
                for case in f['floor_cases']:
                    original=copy.deepcopy(case['inputs'])
                    calculated=engine.run_calculation(case['inputs'],'QA_FLOOR',{'山东仓':(8,5)},case['exception'],case['budget'],case['profile'])
                    row=calculated['rows'][0];actual={k:row.get(k) for k in ('AQ','BI','AR','BP','BQ','AS')}
                    actual.update(execution=sem.order_quantity(row),short_reason=normal_exports.decision(row))
                    require(actual==case['expected'] and case['inputs']==original,'真实程序断货规则符合既定对照且不改输入：'+case['name'])
                    floor_results.append({'name':case['name'],**actual})
            with closing(sqlite3.connect(path)) as db:require('\n'.join(db.iterdump())==before,'本轮窗口、预览和纯函数未修改主业务库')
            report={'passed':True,'checks':checks,'floor_results':floor_results,'synthetic':True,'live_platform':False,**diagnostic()}
        except Exception:report={'passed':False,'checks':checks,'error':traceback.format_exc(),'live_platform':False,**diagnostic()}
        for d in dialogs:d.close();d.deleteLater()
        (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8',newline='\n')
        win._discard_close=True;win.job_dirty=False;app.exit(0 if report['passed'] else 1)
    QTimer.singleShot(300,run)
