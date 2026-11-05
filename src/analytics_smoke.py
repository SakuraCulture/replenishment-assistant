"""Actual packaged Qt/spawn analytics acceptance, isolated synthetic data only."""
import os,sys,time,json,traceback,hashlib
from pathlib import Path
from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest

def start_smoke(app,win,args):
    out=Path(args[0]).resolve();beats=[]
    def wait(test,seconds=120):
        end=time.monotonic()+seconds
        while test() and time.monotonic()<end:QTest.qWait(10)
        if test():raise AssertionError('后台任务未按时结束')
    def execute():
        timer=QTimer(win);timer.setInterval(25);timer.timeout.connect(lambda:beats.append(time.monotonic()));timer.start()
        try:
            if Path(os.environ.get('REPLENISHMENT_DATA_DIR','')).resolve()!=out/'settings':raise ValueError('必须指定独立QA/settings')
            win.auto_check.stop();win.jst_generator.stopped=True;win.jst_generator.timer.stop();win.aoxiang_workflows.stopping=True;win.aoxiang_workflows.timer.stop()
            win.show_section(3);win.analytics.parentWidget().setCurrentWidget(win.analytics) if hasattr(win.analytics.parentWidget(),'setCurrentWidget') else None
            widget=win.analytics;widget.query();wait(lambda:widget.worker is not None)
            if widget.report is None or not widget.report['rows']:raise AssertionError('实际EXE查询未返回合成订单')
            app.processEvents();widget.grab().save(str(out/'经营分析.png'))
            widget.open_detail('store',0);widget.open_detail('warehouse',0);widget.open_detail('order',0)
            if not widget.models['detail'].rowCount():raise AssertionError('下钻丢失商品')
            widget.grab().save(str(out/'商品漏斗.png'));widget.back()
            import order_analytics_exports as exports
            receipts=[];widget.start_task(exports.export,(win.work_repository.path,widget.report,out/'actual-exe-report.xlsx'),receipts.append);wait(lambda:widget.worker is not None)
            if not receipts or not Path(receipts[0]['path']).is_file():raise AssertionError('实际EXE报表未写出')
            widget.query();widget.cancel();wait(lambda:widget.worker is not None);widget.query();wait(lambda:widget.worker is not None)
            from retry_profile_smoke import install
            install(win,'B');win.auto_check.stop();win.concurrency.setValue(2)
            g=win.automation;g.enable();task=g.start_stock_diagnosis({'operation_key':'exe-trial','context_token':g.token(),'stores':['S1','S2'],'categories':['快销']});wait(lambda:g.worker is not None)
            status=g.task_record(task['task_id'])
            if status['state']!='completed':raise AssertionError(status)
            report=g.stock_diagnosis_results({'task_id':task['task_id'],'limit':100})
            if report['hard_issues']:raise AssertionError(report['hard_issues'])
            result=g.adopt_stock_diagnosis({'task_id':task['task_id'],'context_token':g.token(),'candidate':1,'reason':'隔离EXE验证','accept_risks':True})
            if 'stock_diagnosis_audit' not in win.results['S1']:raise AssertionError('采用审计未进入结果')
            g.undo_stock_diagnosis({'task_id':task['task_id'],'context_token':result['context_token']})
            from stock_diagnosis_ui import DiagnosisDialog
            dialog=DiagnosisDialog(win);dialog.session=g.diagnosis_sessions[task['task_id']];dialog.fill_candidates();dialog.candidate.setCurrentIndex(1);dialog.show();app.processEvents();dialog.grab().save(str(out/'安全库存诊断.png'));dialog.accept();dialog.deleteLater()
            from version_info import diagnostic
            answer={'passed':True,'frozen_exe':bool(getattr(sys,'frozen',False)),**diagnostic(),'rows':len(widget.report['rows']),
                    'exports':receipts,'candidate_count':report['candidate_count'],'heartbeat_ticks':len(beats),
                    'maximum_heartbeat_gap_seconds':max((b-a for a,b in zip(beats,beats[1:])),default=0),
                    'operations':['原生查询','店仓订单商品下钻','全量导出','停止和重试','双进程冻结试算','采用及审计','撤回','关闭诊断窗'],
                    'limits':'合成数据；未连接外部AI客户端或平台；自动Qt交互，不替代业务人员自然操作验收'}
        except Exception:answer={'passed':False,'error':traceback.format_exc(),'frozen_exe':bool(getattr(sys,'frozen',False))}
        timer.stop();(out/'exe-report.json').write_text(json.dumps(answer,ensure_ascii=False,indent=2,default=str),'utf-8');win._discard_close=True;win.job_dirty=False;app.exit(0 if answer['passed'] else 1)
    QTimer.singleShot(250,execute)
