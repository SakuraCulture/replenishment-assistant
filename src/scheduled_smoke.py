"""Opt-in actual Qt/spawn acceptance; only explicit isolated synthetic inputs."""
import copy,json,os,sys,time,traceback
from pathlib import Path


def fixture(root):
    from openpyxl import Workbook
    import work_repository as wr,warehouse_catalog as wc,store_parameter_import as spi,replenishment_plans as plans
    work=wr.Repository(root);catalogs={};files=[]
    for wh in ('山东仓','河北仓'):
        p=root/(wh+'.xlsx');b=Workbook();w=b.active;w.append(wc.REQUIRED+['商品标签','商品名称'])
        w.append(['0001','饮品',2,6,'启用',100,'','合成饮品']);b.save(p);b.close()
        catalogs[wh]=wc.import_catalog(p,wh);wc.Repository(root).save(catalogs[wh])
    for code in ('A','B'):
        r=spi.base();r['ui'].update(mode=1,primary='河北仓' if code=='A' else '山东仓',secondary=None);work.save_stores({code:r})
        p=root/(code+'.xlsx');b=Workbook();w=b.active;w.append(['仓库/门店编码','仓库/门店名称','商品编码','商品名称','日销','周销','月销','可用库存','在途库存','缺货天数（天）','补货状态']);w.append([code,'测试'+code,'0001','合成饮品',2,14,60,0,0,0,'正常']);b.save(p);b.close()
        from input_adapter import digest
        files.append({'file':str(p),'warehouse_code':code,'sha256':digest(p)})
    plan=plans.Repository(work.path).save({'name':'合成早班','stores':[{'code':c,'name':'测试'+c} for c in ('A','B')],'config':spi.base()['preset']['config'],'parameter_fields':[]})
    return plan,catalogs,{'files':files}


class SyntheticPlatforms:
    def __init__(self,catalogs,manifest,cancel):self.data=catalogs;self.manifest=manifest;self.cancel=cancel
    def acquire(self):pass
    def preflight(self,basis,progress):
        progress('合成连接核验，不访问平台');time.sleep(.15);return {'message':'合成核验通过'}
    def sales(self,runner,basis):return self.manifest
    def catalogs(self,runner,basis):
        for w,c in self.data.items():runner.repo.save_value(runner.rid,'catalog:'+w,c,runner.epoch)
        return self.data
    def release(self,progress):pass


def start_smoke(app,win,args):
    from PySide6.QtCore import QTimer,QEventLoop
    from PySide6.QtWidgets import QMessageBox
    from version_info import diagnostic
    import scheduled_ui as ui,scheduled_execution as execution,scheduled_replenishment as sr
    out=Path(args[0]).resolve()
    if not (out/'synthetic.json').is_file() or Path(os.environ.get('REPLENISHMENT_DATA_DIR','')).resolve()!=out/'user':raise ValueError('需要专用合成验收目录')
    app.setQuitOnLastWindowClosed(False)
    def run():
        report={'passed':False,'frozen':bool(getattr(sys,'frozen',False)),'version':diagnostic(),'checks':[]};checks=report['checks'];beats=[];dialog=None;phase='test_setup'
        m=win.scheduled;original=m.platform;originalask=ui.ask
        def require(v,text):
            if not v:raise AssertionError(text)
            checks.append(text)
        def wait(fn,seconds=45):
            end=time.monotonic()+seconds
            while not fn() and time.monotonic()<end:
                loop=QEventLoop();QTimer.singleShot(20,loop.quit);loop.exec()
            if not fn():raise AssertionError('等待超时')
        timer=QTimer();timer.setInterval(50);timer.timeout.connect(lambda:beats.append((phase,time.monotonic())));timer.start()
        try:
            win.jst_generator.timer.stop();win.aoxiang_workflows.timer.stop();m.timer.stop();wait(lambda:not m.reader)
            plan,catalogs,manifest=fixture(out/'user');m.platform=lambda c:SyntheticPlatforms(catalogs,manifest,c)
            win.job_dirty=True  # Unsaved work must postpone default publication.
            before=win.snapshot();phase='actual_ui';m.open();dialog=m.dialog;wait(lambda:not dialog.worker);dialog.edit();editor=dialog.editor
            editor.name.setText('合成早班');editor.verify();wait(lambda:not editor.worker);require(editor.verified is not None,'运行条件在后台核验并绑定设置')
            editor.save(True);wait(lambda:dialog.editor is None);dialog.refresh();wait(lambda:not dialog.worker)
            require(len(m.repo.schedules())==1 and m.repo.schedules()[0]['enabled'],'实际表单保存并启用安排')
            dialog.grab().save(str(out/'01-执行安排.png'))
            ui.ask=lambda *a:True;dialog.arrangements.selectRow(0);dialog.manual();wait(lambda:m.worker is None and any(r['state']=='COMPLETE' for r in m.repo.list()['items']),60)
            m.timer.stop();wait(lambda:not m.reader and not m.publisher);dialog.refresh();wait(lambda:not dialog.worker)
            r=m.repo.list()['items'][0];require(r['state']=='COMPLETE','实际spawn计算两家门店并逐店保存结果')
            require(win.snapshot()==before,'后台计算没有改动现有工作台')
            require(win.work_repository.list_jobs()==[],'自动任务不覆盖每日草稿')
            require(not m.repo.get(r['id']).get('publication'),'未保存工作台阻止自动更新默认资料')
            win.job_dirty=False;m.poll();wait(lambda:not m.reader and not m.publisher and m.repo.get(r['id']).get('publication'))
            require(m.repo.get(r['id'])['publication']['state']=='PUBLISHED','空闲时采用本任务资料为默认，结果保持')
            dialog.tabs.setCurrentIndex(1);dialog.runs.selectRow(0);dialog.select();app.processEvents();dialog.grab().save(str(out/'02-运行记录.png'))
            dialog.open_result();wait(lambda:win.loaded is not None and not dialog.worker)
            require(win.sections.currentIndex()==1 and win.pages.currentIndex()==3,'两次点击进入原核对与签发页')
            require(list(win.results)==['A','B'] and all(x['state']=='已完成' for x in win.states.values()),'门店顺序与完成状态恢复')
            basis=m.repo.value(r['id'],'basis');require(win._calculation_profile==basis['profile'],'恢复重建必卖清单计算缓存')
            for s in win.results:require(win.selected_settings(s)['parameter_config']==basis['jobs'][s]['parameter_config'],'当时门店参数一致 '+s)
            win.grab().save(str(out/'03-核对与签发.png'))
            phase='direct_test_services'
            beats.append((phase,time.monotonic()))
            from xlsx_exports import export_batch
            export_batch(win.results,win.states,out/'内部明细.xlsx');require((out/'内部明细.xlsx').is_file(),'通过原导出服务生成内部明细')
            from work_repository import summary
            payload=win.snapshot();require(summary(payload)['completed']==2,'原每日作业快照可保存')
            win.work_repository.save_job(r['day'],payload);saved=win.work_repository.get_job(r['day']);require(saved['day']==r['day'],'通过原存储保存并回读每日草稿')
            # Reopen uses the same persisted run; no browser or recalculation.
            m.repo.request_pause(r['id'],True);ended=m.repo.get(r['id']);m.repo.delete_run(r['id'],ended['revision'])
            require(win.work_repository.get_job(r['day']) is not None,'删除自动记录不删除已保存草稿')
            # Exercise real Qt close/cancel coordination with a blocking read-only adapter.
            class Waiting(SyntheticPlatforms):
                def sales(self,runner,basis):
                    from runtime_support import checkpoint
                    runner.update('获取补货建议','合成等待，供停止和关闭验收')
                    while not self.cancel.wait(.05):pass
                    checkpoint(self.cancel)
            win.loaded=None;win._discard_close=True;win.job_dirty=False;phase='actual_ui'
            beats.append((phase,time.monotonic()))
            m.platform=lambda c:Waiting(catalogs,manifest,c);next_run=m.repo.manual(m.repo.schedules()[0]);m.start(next_run)
            wait(lambda:m.repo.get(next_run['id'])['stage']=='获取补货建议');m.pause(next_run['id'])
            wait(lambda:m.worker is None);require(m.repo.get(next_run['id'])['state']=='PAUSED','运行中暂停能到达安全检查点，进程不遗留')
            current=m.repo.get(next_run['id']);m.repo.resume(current['id'],current['revision']);m.platform=lambda c:SyntheticPlatforms(catalogs,manifest,c);m.start(m.repo.get(current['id']))
            wait(lambda:m.worker is None and m.publisher is None and m.reader is None);require(m.repo.get(current['id'])['state']=='COMPLETE','暂停后继续同一任务，取消状态不串入新轮次')
            m.repo.request_pause(current['id'],True)
            last=m.repo.manual(m.repo.schedules()[0]);m.platform=lambda c:Waiting(catalogs,manifest,c);m.start(last)
            wait(lambda:m.repo.get(last['id'])['stage']=='获取补货建议');win.close();wait(lambda:m.worker is None and not win.isVisible())
            require(m.repo.get(last['id'])['state']=='PAUSED','实际窗口关闭前安全暂停，保留原任务供重启恢复')
            intervals=[(b[1]-a[1])*1000 for a,b in zip(beats,beats[1:]) if a[0]==b[0]=='actual_ui']
            ordered=sorted(intervals)
            report.update(passed=True,ui_ticks=len(beats),ui_intervals_ms=intervals,ui_max_gap_ms=max(intervals,default=0),ui_p95_gap_ms=ordered[max(0,__import__('math').ceil(len(ordered)*.95)-1)] if ordered else 0,
                heartbeat_scope='50ms timer in actual UI interactions; fixture creation and direct verification-only export/save calls excluded and not represented as UI operations',
                scope='真实Qt、spawn及本地持久化；平台仅使用明确合成适配器，无真实导出')
        except Exception:report['error']=traceback.format_exc()
        finally:
            ui.ask=originalask;m.platform=original;timer.stop()
            if dialog:dialog.timer.stop()
            (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
            win._discard_close=True;win.close();QTimer.singleShot(700,lambda:app.exit(0 if report['passed'] else 1))
    QTimer.singleShot(500,run)
