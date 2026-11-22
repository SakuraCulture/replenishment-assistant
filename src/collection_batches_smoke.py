"""Opt-in GUI acceptance with an isolated real component and offline platform replies."""
import json
import os
import sys
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QEventLoop, QTimer

from aoxiang_acquisition import manifest
from aoxiang_fetch_ui import FetchDialog
from aoxiang_input_ui import adopt
from aoxiang_jobs import Jobs
from version_info import diagnostic


def start_smoke(app, win, args):
    root=Path(args[0]).resolve()
    mode=args[1]
    settings=root/'settings'
    safe=json.loads((root/'synthetic.json').read_text('utf-8'))
    if (safe!={'synthetic':True,'live_platform':False} or Path(app.data_root).resolve()!=settings
            or Path(os.environ.get('AX_BATCH_CASE','')).resolve()!=root or not os.environ.get('NODE_OPTIONS')):
        raise ValueError('只能在指定的隔离验收资料和回执替身中运行')
    fixture=json.loads((root/'platform.json').read_text('utf-8'))
    component=settings/'aoxiang/component-state'
    codes=list(fixture['names'])
    jobs=Jobs(settings/'aoxiang/jobs.sqlite3')
    dialog=None
    errors=[]

    def wait(predicate, label='', seconds=65):
        deadline=time.monotonic()+seconds
        while not predicate():
            if time.monotonic()>deadline:
                raise AssertionError((label,[(x['id'],x['state'],x['last_error'],(x['record'] or {}).get('error'))
                                             for x in jobs.recent()],errors))
            loop=QEventLoop()
            QTimer.singleShot(25,loop.quit)
            loop.exec()

    def submissions():
        file=root/'platform.log.jsonl'
        events=[json.loads(line) for line in file.read_text('utf-8').splitlines()] if file.exists() else []
        return [event for event in events if event['type']=='submit']

    def close_fetch():
        nonlocal dialog
        d,dialog=dialog,None
        if d is not None:
            d.close()
            wait(lambda:not d.isVisible(),'关闭采集窗口')
            d.deleteLater()

    def fetch():
        nonlocal dialog
        dialog=FetchDialog(win,settings,fixture['day'])
        dialog.show()
        wait(lambda:dialog.worker is None and bool(dialog.configuration),'采集窗口初始化')
        assert dialog._control_only
        return dialog

    def select(cid):
        dialog.history.setCurrentIndex(dialog.history.findData(cid))
        wait(lambda:dialog.worker is None and dialog.current and dialog.current['id']==cid,'查看对应批次')
        return dialog.current

    def new(policy,selected):
        for i,check in enumerate(dialog.checks):
            check.setChecked(dialog.stores.item(i,2).text() in selected)
        dialog.policy.setCurrentIndex(dialog.policy.findData(policy))
        prev=(dialog.current or {}).get('id')
        assert dialog.start_btn.isEnabled()
        dialog.start_btn.click()
        wait(lambda:dialog.worker is None and dialog.current and dialog.current['id']!=prev,'界面新建采集')
        return dialog.current['id']

    def state(cid,want):
        if not dialog.current or dialog.current['id']!=cid:
            select(cid)
        until=time.monotonic()+65
        while dialog.current['state']!=want:
            if time.monotonic()>until:
                raise AssertionError((cid,want,dialog.current,errors))
            if not dialog.worker:
                dialog.refresh_btn.click()
            wait(lambda:dialog.worker is None,'刷新指定批次')
            loop=QEventLoop()
            QTimer.singleShot(50,loop.quit)
            loop.exec()
        return jobs.get(cid)

    def result(cid):
        record=jobs.get(cid)
        value=manifest(record,component)
        return {'id':cid,'state':record['state'],'files':value['files'],'day':record['context']['day']}

    def resume(cid):
        select(cid)
        assert dialog.resume_btn.isEnabled()
        dialog.resume_btn.click()
        return state(cid,'SUCCEEDED')

    def catalog():
        import warehouse_catalog as wc
        master=wc.import_catalog(root/'catalog.xlsx','山东仓')
        wc.Repository(settings).save(master)
        win.catalogs={'山东仓':master}
        return master

    def cross_day():
        import replenishment_plans as plans
        import scheduled_replenishment as sr
        import store_parameter_import as spi
        from scheduled_execution import check_signature, freeze
        from scheduled_platforms import Platforms

        original=fixture['original']['id']
        master=catalog()
        for code in codes:
            record=spi.base()
            record['ui'].update(mode=1,primary='山东仓',secondary=None)
            win.work_repository.save_stores({code:record})
        stores=[{'code':code,'name':fixture['names'][code]} for code in codes]
        plan=plans.Repository(win.work_repository.path).save({
            'name':'三家采集验收','stores':stores,'config':spi.base()['preset']['config'],'parameter_fields':[]})
        manager=win.scheduled
        schedule={'name':'测试','kind':'once','date':fixture['day'],'time':'12:00','workers':1,
                  'plan_id':plan['id'],'plan_name':plan['name'],'stores':stores,'enabled':True}
        schedule['check_signature']=check_signature(freeze(settings,schedule,fixture['day']))
        at=datetime.fromisoformat(fixture['day']+'T12:00:00+08:00')
        manager.repo.clock=lambda:at-timedelta(minutes=1)
        manager.repo.save_schedule(schedule)
        manager.repo.clock=lambda:at+timedelta(seconds=3)
        win.job_dirty=True
        manager.open()
        with patch.object(Platforms,'catalogs',lambda *_:{'山东仓':master}):
            manager.poll()
            wait(lambda:bool(manager.repo.list()['items']) and not manager.worker
                 and manager.repo.list()['items'][0]['state'] not in {'QUEUED','RUNNING'},'实际定时执行')
        run=manager.repo.list()['items'][0]
        assert run['trigger']=='timer' and run['state']=='COMPLETE',run
        assert [s['state'] for s in run['stores']]==['READY']*3,run
        window=manager.dialog
        window.tabs.setCurrentIndex(1);window.refresh()
        wait(lambda:window.worker is None,'读取实际定时结果')
        window.grab().save(str(root/'定时3家完成.png'))
        window.close()
        assert jobs.get(original)['record']==fixture['host_original']
        fresh=result(run['external']['ax'])
        assert [f['warehouse_code'] for f in fresh['files']]==codes
        assert len(submissions())==3
        fetch()
        resume(original)
        assert len(submissions())==3
        return {'scheduled':run,'new':fresh,'old':result(original),'old_before':'PAUSED'}

    def same_day():
        catalog()
        original=fixture['original']['id']
        fetch()
        fresh=new('refresh',codes[:1]);state(fresh,'SUCCEEDED')
        before=result(fresh)
        current=dialog
        current.adopt_btn.click()
        wait(lambda:current.adoption is not None and not current.isVisible(),'采用新批次')
        close_fetch()
        assert adopt(win,current.adoption)
        wait(lambda:win.worker is None,'新文件正常检查')
        adopted=list(win.sales.paths)
        assert adopted==[before['files'][0]['file']]
        assert jobs.get(original)['state']=='PAUSED'
        fetch();resume(original)
        old=result(original)
        reuse=new('reuse',codes[:1]);state(reuse,'SUCCEEDED')
        again=result(reuse)
        assert before==result(fresh) and win.sales.paths==adopted
        assert old['files'][0]['sha256']!=before['files'][0]['sha256']
        assert again['files'][0]['sha256']==before['files'][0]['sha256'] and again['files'][0]['reused']
        assert len(submissions())==1
        return {'old':old,'new':before,'reuse':again,'adopted':adopted}

    def busy():
        fetch()
        blocker=new('refresh',codes[:1])
        wait(lambda:len(submissions())==1,'第一笔已在浏览器处理')
        queued=new('refresh',codes[:1]);state(queued,'QUEUED')
        assert '等待浏览器可用' in dialog.status.text() and len(submissions())==1
        dialog.grab().save(str(root/'等待浏览器可用.png'))
        for _ in range(2):
            receipt=dialog.client.request('resume',command_id=queued)
            assert receipt['id']==queued and receipt['state']=='QUEUED'
        (root/'hold-12332700').unlink()
        state(queued,'SUCCEEDED')
        select(blocker);state(blocker,'SUCCEEDED')
        assert len(submissions())==2
        return {'old':result(blocker),'new':result(queued),'queued':queued}

    def reopen():
        fetch()
        path=root/'queued-id.txt'
        if mode=='restart-before':
            ident=new('refresh',codes[:1])
            state(ident,'QUEUED')
            path.write_text(ident,encoding='utf-8')
            assert not submissions()
            return {'queued':ident,'before_submissions':0}
        ident=path.read_text('utf-8')
        state(ident,'QUEUED')
        assert not submissions()
        (root/'browser-busy').unlink()
        state(ident,'SUCCEEDED')
        assert len(submissions())==1
        return {'queued':ident,'new':result(ident)}

    def unknown():
        original=fixture['original']['id']
        fetch();old=select(original)
        assert old['state']=='NEEDS_REVIEW' and dialog.resume_btn.isEnabled()
        dialog.resume_btn.click()
        wait(lambda:dialog.worker is None,'核对未知原提交')
        assert jobs.get(original)['record']==old['record'] and not submissions()
        assert errors and '原任务尚无可靠受理回执' in errors[-1]
        first=new('refresh',codes[:1]);state(first,'SUCCEEDED')
        reused=new('reuse',codes[:1]);state(reused,'SUCCEEDED')
        refreshed=new('refresh',codes[:1]);state(refreshed,'SUCCEEDED')
        assert jobs.get(original)['record']==old['record'] and len(submissions())==2
        a,b,c=map(result,(first,reused,refreshed))
        assert b['files'][0]['sha256']==a['files'][0]['sha256'] and b['files'][0]['reused']
        assert c['files'][0]['task_id']!=a['files'][0]['task_id']
        return {'old':{'id':original,'state':old['state'],'files':[],'day':old['context']['day']},
                'first_refresh':a,'reuse':b,'new':c}

    def run():
        output={'passed':False,'scenario':mode,'version':diagnostic(),'frozen':bool(getattr(sys,'frozen',False))}
        try:
            win.jst_generator.timer.stop()
            win.scheduled.timer.stop()
            win.aoxiang_workflows.timer.stop()
            wait(lambda:not win.scheduled.reader,'应用启动')
            with patch('readable_ui.failure',side_effect=lambda *args:errors.append(args[-1])):
                fn={'cross':cross_day,'same':same_day,'busy':busy,'unknown':unknown,
                    'restart-before':reopen,'restart-after':reopen}[mode]
                output.update(fn(),passed=True)
                assert not errors or mode=='unknown',errors
        except Exception:
            output.update(passed=False,error=traceback.format_exc())
        finally:
            try:
                close_fetch()
                assert win.aoxiang_workflows.session.close(timeout=10),'隔离组件未安全退出'
            except Exception:
                output.update(passed=False,cleanup_error=traceback.format_exc())
            output.update(submissions=submissions(),ui_errors=errors)
            (root/(mode+'-report.json')).write_text(json.dumps(output,ensure_ascii=False,indent=2),encoding='utf-8')
            win.job_dirty=False
            win._discard_close=True
            app.exit(0 if output['passed'] else 1)

    QTimer.singleShot(0,run)
