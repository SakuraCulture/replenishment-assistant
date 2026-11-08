"""Opt-in isolated Qt export acceptance for the three stored order identifiers."""
import json,hashlib,time,traceback
from pathlib import Path
from unittest.mock import patch
from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest
from version_info import diagnostic


def start_smoke(app,win,args):
    out=Path(args[0]);checks=[]
    def require(ok,title):
        if not ok:raise AssertionError(title)
        checks.append(title)
    def wait(test):
        until=time.monotonic()+90
        while test() and time.monotonic()<until:QTest.qWait(20)
        if test():raise AssertionError('隔离Qt任务超时')
    def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
    def execute():
        try:
            import openpyxl,order_analytics as service,analytics_identifiers as ids,payment_references as refs
            path=win.work_repository.path;require(path.resolve()==(out/'settings/workbench.sqlite3').resolve(),'仅使用本轮隔离数据库')
            win.auto_check.stop();win.jst_generator.stopped=True;win.jst_generator.timer.stop();win.aoxiang_workflows.stopping=True;win.aoxiang_workflows.timer.stop()
            wait(lambda:win.jst_generator.worker is not None)
            f=json.loads((out/'fixture.json').read_text('utf-8'));component=path.parent/'aoxiang/component-state/state.sqlite'
            before=sha(path),sha(component)
            win.show_section(3);w=win.analytics
            if hasattr(w.parentWidget(),'setCurrentWidget'):w.parentWidget().setCurrentWidget(w)
            w.start.setText(f['from']);w.end.setText(f['to']);w.query();wait(lambda:w.worker is not None)
            require(w.report is not None and len(w.report['identifiers'])==4,'两天同店两仓四张签发单全部纳入')
            snapshot=w.report;by_order={r['order']:r['references'] for r in snapshot['identifiers']}
            require(all(by_order[e['order']]['jst_numbers']==[e['jst']] for e in f['expected']),'本机维护的聚水潭内部号按原仓单带入快照')
            require(all([p['number'] for p in by_order[e['order']]['purchases']]==([e['po']] if e['po'] else []) for e in f['expected']),'翱象采购号按原签发身份逐单对应，不按同店串单')
            for index,key in enumerate(('store','warehouse','order')):
                w.tabs.setCurrentIndex(index);require(w.models[key].headers[2:5]==ids.HEADERS,'界面三种编号并列：'+key)
            w.tabs.setCurrentIndex(2);app.processEvents();w.grab().save(str(out/'订单三种单号.png'))
            w.back();app.processEvents();w.grab().save(str(out/'门店经营分析单号.png'))
            receipts=[]
            with patch('readable_ui.file_results',side_effect=lambda parent,files,*args,**kwargs:receipts.extend(files)):
                w.export_btn.click();wait(lambda:w.worker is not None)
            require(len(receipts)==1 and Path(receipts[0]).is_file(),'实际点击导出当前范围保存经营分析文件')
            full=Path(receipts[0]);book=openpyxl.load_workbook(full)
            try:
                for name in ('门店经营对比','门店分仓对比','订单五段漏斗'):
                    require(book[name].max_column==45 and list(next(book[name].values))[-3:]==ids.HEADERS,'导出追加三种单号，原指标位置保留：'+name)
                records=list(book['单号关联明细'].values)[1:]
                require({(r[4],r[5],r[6]) for r in records}=={(e['order'],e['jst'],e['po']) for e in f['expected']},'单号关联明细保存两日两仓的完整对应')
                require(any(r[8]=='已作废' for r in records),'已作废采购单保留原号和已留存状态')
                require(all(c.data_type=='s' for row in list(book['单号关联明细'])[1:] for c in row[4:7] if c.value is not None),'三种编号按文本保存，聚水潭号前导零保留')
            finally:book.close()
            w.tabs.setCurrentIndex(2);w.tables['order'].selectRow(0);chosen=next(iter(w.selected));selected=[]
            with patch('readable_ui.file_results',side_effect=lambda parent,files,*args,**kwargs:selected.extend(files)):
                w.selected_btn.click();wait(lambda:w.worker is not None)
            book=openpyxl.load_workbook(selected[0])
            try:require({r[4] for r in list(book['单号关联明细'].values)[1:]}=={chosen},'勾选导出只保存所选原单的对应关系')
            finally:book.close()
            require((sha(path),sha(component))==before,'查询和两次导出未修改业务库或翱象组件库')
            head=next(h for h in snapshot['heads'] if h['number']==chosen);refs.save(path,refs.read(path,head),'000-UPDATED')
            w.check_update();wait(lambda:w.basis_worker is not None)
            require(w.stale and not w.export_btn.isEnabled() and w.report is snapshot,'补号变化使原快照待刷新，未静默混入旧报表')
            w.query();wait(lambda:w.worker is not None)
            require(w.report['snapshot_id']!=snapshot['snapshot_id'] and w.report['metrics']==snapshot['metrics'],'刷新后更新单号快照，五段指标保持相同')
            w.metric_choice.setCurrentIndex(3);require(w.models['order'].headers[2:5]==ids.HEADERS,'可售天数视图同样保留三种单号')
            result={'passed':True,'checks':checks,'workbook':str(full),'live_platform':False,'live_database_written':False,'synthetic':True,**diagnostic()}
        except Exception:result={'passed':False,'checks':checks,'error':traceback.format_exc(),'live_platform':False,**diagnostic()}
        (out/'report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),'utf-8')
        win._discard_close=True;win.job_dirty=False;app.exit(0 if result['passed'] else 1)
    QTimer.singleShot(300,execute)
