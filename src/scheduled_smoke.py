"""Opt-in actual Qt/spawn acceptance; only explicit isolated synthetic inputs."""
import copy,json,os,sys,time,traceback
from pathlib import Path
from types import SimpleNamespace
from scheduled_platforms import Platforms


def fixture(root,codes=('A','B'),name='合成早班'):
    from openpyxl import Workbook
    import work_repository as wr,warehouse_catalog as wc,store_parameter_import as spi,replenishment_plans as plans
    work=wr.Repository(root);catalogs={};files=[]
    for wh in ('山东仓','河北仓'):
        p=root/(wh+'.xlsx');b=Workbook();w=b.active;w.append(wc.REQUIRED+['商品标签','商品名称'])
        w.append(['0001','饮品',2,6,'启用',100,'必卖清单' if wh=='河北仓' else '','合成饮品']);b.save(p);b.close()
        catalogs[wh]=wc.import_catalog(p,wh);wc.Repository(root).save(catalogs[wh])
    records={}
    for i,code in enumerate(codes):
        r=spi.base();r['ui'].update(mode=1,primary='河北仓' if i%2==0 else '山东仓',secondary=None);records[code]=r
        p=root/(code+'.xlsx');b=Workbook();w=b.active;w.append(['仓库/门店编码','仓库/门店名称','商品编码','商品名称','日销','周销','月销','可用库存','在途库存','缺货天数（天）','补货状态']);w.append([code,'测试'+code,'0001','合成饮品',2,14,60,0,0,0,'正常']);b.save(p);b.close()
        from input_adapter import digest
        files.append({'file':str(p),'warehouse_code':code,'sha256':digest(p)})
    work.save_stores(records)
    plan=plans.Repository(work.path).save({'name':name,'stores':[{'code':c,'name':'测试'+c} for c in codes],'config':spi.base()['preset']['config'],'parameter_fields':[]})
    return plan,catalogs,{'files':files}


class SyntheticPlatforms(Platforms):
    def __init__(self, repo, catalogs, manifest, cancel, bindings):
        def read_configuration(action):
            if action != 'configuration':
                raise AssertionError(action)
            bindings['calls'].append(action)
            return {'configured': True, 'enabled': True, 'targets': [
                {'buyerWarehouseCode': item['warehouse_code'], 'mappingVerifiedAt': '2026-11-04'}
                for item in manifest['files'] if item['warehouse_code'] != bindings.get('missing_store')
            ]}

        def read_catalog_status():
            bindings['calls'].append('sync.status')
            return {'accounts': [
                {'warehouse': warehouse, 'enabled': True, 'mappingVerified': True,
                 'expectedCompany': '合成' + warehouse, 'expectedCoid': str(i), 'expectedUid': '1'}
                for i, warehouse in enumerate(catalogs, 1)
            ]}

        def control_lease(*, cancelled, control_only):
            if not control_only:
                raise AssertionError('读取配置不应占用翱象浏览器')
            return SimpleNamespace(start=lambda: None, close=lambda: True, request=read_configuration)

        ax = SimpleNamespace(session=SimpleNamespace(lease=control_lease))
        catalog = SimpleNamespace(read_status=read_catalog_status)
        super().__init__(repo.path.parent, repo.path, ax, catalog, cancel)
        self.data = catalogs
        self.manifest = manifest

    def sales(self,runner,basis):return self.manifest
    def catalogs(self,runner,basis):
        for w,c in self.data.items():runner.repo.save_value(runner.rid,'catalog:'+w,c,runner.epoch)
        return self.data
    def release(self,progress):pass


class ControlledCollection:
    """Protocol fixture for the EXE: real scheduling/validation, controlled export receipts."""
    def __init__(self,folder,manifest):
        self.folder=folder
        self.sources={file['warehouse_code']:file for file in manifest['files']}
        self.codes=list(self.sources)
        self.unknown,self.failed,self.accepted,self.unsent=self.codes[-4:]
        self.config={'configured':True,'enabled':True,'account_binding':{'account_id':'test','merchant_code':'test'},
                     'targets':[{'buyerWarehouseCode':code,'mappingVerifiedAt':'2026-11-25'} for code in self.codes],
                     'store_mapping_version':'0'*64}
        self.receipt=None;self.scopes=[];self.submits=[];self.queries=[];self.late_receipt=False

    def succeed(self,item):
        import shutil
        code=item['warehouse_code'];source=self.sources[code];original=Path(source['file'])
        path=self.folder/'component-state'/'command-files'/self.receipt['id']/f"{item['index']}-{source['sha256']}.xlsx"
        path.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(original,path)
        item.update(state='SUCCEEDED',error=None,result={**source,'file':str(path),'run_id':item['run_id'],
                    'account_binding':self.config['account_binding'],'bytes':path.stat().st_size,'task_id':item['task_id']})

    def request(self,action,**values):
        from aoxiang_jobs import fingerprint
        if action=='configuration':return self.config
        if action=='reserve':
            command=values['command'];codes=command['scope']['warehouse_codes']
            assert codes==self.codes and self.receipt is None
            self.receipt={'id':command['command_id'],'fingerprint':fingerprint(command),'request':command,
                          'sequence':1,'state':'NEEDS_REVIEW','items':[]}
            for index,code in enumerate(codes):
                item={'index':index,'warehouse_code':code,'run_id':'run-'+code,'result':None,
                      'task_id':None if code in (self.unknown,self.unsent) else 'original-'+code}
                if code!=self.unsent:self.submits.append([code,'original-'+code])
                if index<25:self.succeed(item)
                else:
                    state,key,message={self.unknown:('NEEDS_REVIEW','SUBMISSION_UNKNOWN','原导出受理待核'),
                        self.failed:('FAILED','TASK_HAS_FAILURES','平台原任务包含失败行'),
                        self.accepted:('INTERRUPTED','TASK_NOT_FOUND','暂未定位已受理原任务'),
                        self.unsent:('PENDING','PAUSED','门店尚未提交')}[code]
                    item.update(state=state,error={'code':key,'message':message})
                self.receipt['items'].append(item)
            return {'record':copy.deepcopy(self.receipt)}
        assert self.receipt is not None and values['command_id']==self.receipt['id'],action
        if action=='status':return copy.deepcopy(self.receipt)
        assert action=='collection.retry_selected',action
        assert values['sequence']==self.receipt['sequence'] and values['retry_id'] not in [r['id'] for r in self.scopes]
        chosen=[self.receipt['items'][i] for i in values['indices']]
        self.scopes.append({'id':values['retry_id'],'stores':[i['warehouse_code'] for i in chosen]})
        for item in chosen:
            code=item['warehouse_code'];assert item['state']!='SUCCEEDED'
            if code==self.unknown and not self.late_receipt:continue
            if code in (self.failed,self.unsent):
                item['task_id']='retry-'+code;self.submits.append([code,item['task_id']])
            else:
                item['task_id']='original-'+code;self.queries.append([code,item['task_id']])
            self.succeed(item)
        self.receipt['sequence']+=1
        self.receipt['state']='SUCCEEDED' if all(i['state']=='SUCCEEDED' for i in self.receipt['items']) else 'NEEDS_REVIEW'
        return copy.deepcopy(self.receipt)


class RetryPlatforms(SyntheticPlatforms):
    def __init__(self,repo,catalogs,manifest,cancel,bindings,collection):
        super().__init__(repo,catalogs,manifest,cancel,bindings)
        self.ax_manager.store=SimpleNamespace(folder=collection.folder)
        self.ax_manager.session.lease=lambda **_:SimpleNamespace(start=lambda:None,close=lambda **_:True,request=collection.request)
        self.warehouse_reads=bindings.get('warehouse_reads',[])

    def sales(self,runner,basis):return Platforms.sales(self,runner,basis)

    def catalogs(self,runner,basis):
        self.warehouse_reads.append(runner.epoch)
        return super().catalogs(runner,basis)

    def release(self,progress):self.release_ax(progress)


def start_smoke(app,win,args):
    from PySide6.QtCore import QTimer,QEventLoop
    from PySide6.QtWidgets import QMessageBox
    from version_info import diagnostic
    import scheduled_ui as ui,scheduled_execution as execution,scheduled_replenishment as sr
    out=Path(args[0]).resolve()
    if not (out/'synthetic.json').is_file() or Path(os.environ.get('REPLENISHMENT_DATA_DIR','')).resolve()!=out/'user':raise ValueError('需要专用合成验收目录')
    app.setQuitOnLastWindowClosed(False)
    def run():
        report={'passed':False,'frozen':bool(getattr(sys,'frozen',False)),'version':diagnostic(),'window_title':win.windowTitle(),'checks':[]};checks=report['checks'];beats=[];dialog=None;phase='test_setup'
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
            plan,catalogs,manifest=fixture(out/'user')
            bindings = {'calls': [], 'missing_store': 'B'}
            m.platform = lambda c: SyntheticPlatforms(m.repo, catalogs, manifest, c, bindings)
            win.job_dirty=True  # Unsaved work must postpone default publication.
            before=win.snapshot();phase='actual_ui';m.open();dialog=m.dialog;wait(lambda:not dialog.worker);dialog.edit();editor=dialog.editor
            editor.name.setText('合成早班');editor.enable.click();wait(lambda:not editor.worker)
            require(not m.repo.schedules() and '测试B（B）' in editor.note.text(),'缺少门店映射时在安排里指明该店')
            bindings.pop('missing_store');bindings['calls'].clear()
            editor.enable.click();wait(lambda:dialog.editor is None);dialog.refresh();wait(lambda:not dialog.worker)
            saved, = m.repo.schedules()
            require(saved['enabled'] and saved.get('reuse_verified_bindings') is True,'点保存并启用即可采用已有核验设置')
            require(bindings['calls']==['configuration','sync.status'],'仅读取两个组件的已保存绑定')
            dialog.arrangements.selectRow(0);dialog.edit_selected();editor=dialog.editor
            editor.workers.setValue(1);editor.enable.click();wait(lambda:dialog.editor is None);dialog.refresh();wait(lambda:not dialog.worker)
            require(m.repo.schedules()[0]['workers']==1,'修改已启用安排直接保存')
            editor_record=win.work_repository.stores()['A']
            editor_record['preset']['config']['warehouses']['河北仓']=[10,2]
            win.work_repository.save_stores({'A':editor_record})
            dialog.grab().save(str(out/'01-执行安排.png'))
            ui.ask=lambda *a:True;dialog.arrangements.selectRow(0);dialog.manual();wait(lambda:m.worker is None and any(r['state']=='COMPLETE' for r in m.repo.list()['items']),60)
            m.timer.stop();wait(lambda:not m.reader and not m.publisher);dialog.refresh();wait(lambda:not dialog.worker)
            r=m.repo.list()['items'][0];require(r['state']=='COMPLETE','实际spawn计算两家门店并逐店保存结果')
            require(m.repo.value(r['id'],'basis')['jobs']['A']['warehouses']['河北仓']==[10,2],
                    '安排保存后调整门店参数，下一次任务采用已保存的新值')
            require(win.snapshot()==before,'后台计算没有改动现有工作台')
            require(win.work_repository.list_jobs()==[],'自动任务不覆盖每日草稿')
            require(not m.repo.get(r['id']).get('publication'),'未保存工作台阻止自动更新默认资料')
            win.job_dirty=False;m.poll();wait(lambda:not m.reader and not m.publisher and m.repo.get(r['id']).get('publication'))
            require(m.repo.get(r['id'])['publication']['state']=='PUBLISHED','空闲时采用本任务资料为默认，结果保持')
            dialog.tabs.setCurrentIndex(1);dialog.runs.selectRow(0);dialog.select();app.processEvents();dialog.grab().save(str(out/'02-运行记录.png'))
            def dismiss_empty_draft():
                pending=app.activeModalWidget()
                if isinstance(pending,QMessageBox) and pending.windowTitle()=='有未保存的作业变更':
                    next(b for b in pending.buttons() if b.text()=='放弃本次变更').click()
            QTimer.singleShot(20,dismiss_empty_draft)
            dialog.open_result();wait(lambda:win.loaded is not None and not dialog.worker)
            require(win.sections.currentIndex()==1 and win.pages.currentIndex()==3,'从运行记录打开原核对与签发页')
            require(list(win.results)==['A','B'] and all(x['state']=='已完成' for x in win.states.values()),'门店顺序与完成状态恢复')
            from priority_list import catalog_profile
            basis=m.repo.value(r['id'],'basis')
            require(win._calculation_profile==catalog_profile(m.repo.value(r['id'],'catalogs')),
                    '恢复重建当批分仓必卖清单计算缓存')
            require(win.results['A']['rows'][0]['_must_sell'] and not win.results['B']['rows'][0]['_must_sell'],
                    '同一商品在两仓用各自的标签识别必采')
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
            m.platform=lambda c:Waiting(m.repo,catalogs,manifest,c,bindings);next_run=m.repo.manual(m.repo.schedules()[0]);m.start(next_run)
            wait(lambda:m.repo.get(next_run['id'])['stage']=='获取补货建议');m.pause(next_run['id'])
            wait(lambda:m.worker is None);require(m.repo.get(next_run['id'])['state']=='PAUSED','运行中暂停能到达安全检查点，进程不遗留')
            current=m.repo.get(next_run['id']);m.repo.resume(current['id'],current['revision']);m.platform=lambda c:SyntheticPlatforms(m.repo,catalogs,manifest,c,bindings);m.start(m.repo.get(current['id']))
            wait(lambda:m.worker is None and m.publisher is None and m.reader is None);require(m.repo.get(current['id'])['state']=='COMPLETE','暂停后继续同一任务，取消状态不串入新轮次')
            m.repo.request_pause(current['id'],True)
            phase='test_setup';codes=tuple(f'R{i:02}' for i in range(1,30))
            retry_plan,retry_catalogs,retry_manifest=fixture(out/'user',codes,'合成29店重试')
            selection={**m.repo.schedules()[0],'name':'合成29店重试','plan_id':retry_plan['id'],
                       'plan_name':retry_plan['name'],'stores':retry_plan['stores']}
            collection=ControlledCollection(out/'user'/'controlled-aoxiang',retry_manifest)
            bindings['warehouse_reads']=[];win.job_dirty=True;phase='actual_ui'
            m.platform=lambda c:RetryPlatforms(m.repo,retry_catalogs,retry_manifest,c,bindings,collection)
            started=m.repo.manual(selection);run_id=started['id'];m.start(started)
            wait(lambda:m.repo.get(run_id)['state']=='REVIEW' and not m.worker,90)
            def run_count():return sum(s['state']=='READY' for s in m.repo.get(run_id)['stores'])
            def pick_run():
                wait(lambda:not dialog.worker);dialog.refresh();wait(lambda:not dialog.worker)
                dialog.tabs.setCurrentIndex(1)
                dialog.runs.selectRow(next(i for i,r in enumerate(dialog.records) if r['id']==run_id));dialog.select()
            require(run_count()==25,'同批首次 25/29，另外四店保留不同原回执')
            saved_results={c:m.repo.value(run_id,'result:'+c) for c in codes[:25]}
            pick_run();dialog.run_actions['结束本次'].click()
            wait(lambda:m.repo.get(run_id)['state']=='ENDED');pick_run()
            dialog.check_failures.click()
            require(dialog.checked_stores==set(codes[-4:]),'原运行已结束仍能一键勾选四家，已完成门店无需重跑')
            dialog.grab().save(str(out/'04-原批次勾选四家.png'))
            action=dialog.run_actions['重试所选失败门店'];action.click();action.click()
            wait(lambda:run_count()==28 and m.repo.get(run_id)['state']=='REVIEW' and not m.worker,90)
            require(len(collection.scopes)==1 and collection.scopes[0]['stores']==list(codes[-4:]),'重复点击只登记一次，同原批次带入四个勾选项')
            remaining=m.repo.get(run_id)['stores'][-4]
            require(remaining['state']=='FAILED' and '受理待核' in remaining['message'],'无唯一原回执的一店显示原原因，其他三店完成')
            pick_run();dialog.grab().save(str(out/'05-保留待核.png'))
            collection.late_receipt=True;dialog.check_failures.click();action.click()
            wait(lambda:m.repo.get(run_id)['state']=='COMPLETE' and not m.worker,90)
            require(run_count()==29 and collection.scopes[-1]['stores']==[codes[-4]],'原回执可核后只接着处理该店，原批次完成 29/29')
            require(all(m.repo.value(run_id,'result:'+c)==v for c,v in saved_results.items()),'原有25家结果逐店完全一致')
            require(bindings['warehouse_reads']==[1] and len(collection.submits)==30 and len(collection.queries)==2,
                    '沿用原两仓资料，新增两个受控导出动作，已受理店均按原号查询')
            require(len(m.repo.value(run_id,'sales')['files'])==29,'原采集编号下的29份已验收文件保持计划顺序')
            report['retry']={'run_id':run_id,'command_id':collection.receipt['id'],'completed':[25,28,29],
                             'scopes':collection.scopes,'submits':collection.submits,'original_queries':collection.queries,
                             'warehouse_reads':bindings['warehouse_reads']}
            pick_run();dialog.grab().save(str(out/'06-原批次全部完成.png'));win.job_dirty=False
            last=m.repo.manual(m.repo.schedules()[0]);m.platform=lambda c:Waiting(m.repo,catalogs,manifest,c,bindings);m.start(last)
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
