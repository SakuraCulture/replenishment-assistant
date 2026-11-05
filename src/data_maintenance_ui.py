"""One permanent-delete preview; no new recycle-bin destinations."""
from PySide6.QtWidgets import QDialog,QVBoxLayout,QHBoxLayout,QCheckBox,QMessageBox
from ui_controls import label,button
from business_ui import run,grid
from runtime_support import Cancelled
from contextlib import nullcontext
from ui_work_io import Session,request
import logging
import data_maintenance as maintenance

def hub(host):
    d=QDialog(host);d.setWindowTitle('数据与任务维护');v=QVBoxLayout(d)
    v.addWidget(label('按要处理的问题选择入口','section'))
    def go(fn):d.accept();fn()
    v.addWidget(button('维护门店默认 / 撤销参数导入',lambda:go(lambda:host.show_section(4))))
    v.addWidget(button('更新或恢复仓商品资料',lambda:go(lambda:(host.show_section(1),host.navigate(0)))))
    v.addWidget(button('查看批次 / 重跑失败任务',lambda:go(host.aoxiang_workflows.open_queue)))
    v.addWidget(button('结束当前计算批次并新建',lambda:go(host.end_batch)))
    v.addWidget(button('查看或删除每日草稿',lambda:go(host.history)))
    v.addWidget(button('查询并彻底删除订单',lambda:go(lambda:host.show_section(2))))
    v.addWidget(button('备份与存储维护',lambda:go(host.storage_copy)))
    v.addWidget(button('关闭',d.reject));d.exec()

def remove_job(host,record):
    return guarded(host,host,lambda:_remove_job(host,record))

def guarded(parent,host,operation):
    generator=getattr(host,'jst_generator',None);manager=getattr(host,'aoxiang_workflows',None)
    if host.worker or (generator and generator.worker) or (manager and manager.worker) or getattr(host,'_aoxiang_dialog',None):
        QMessageBox.information(parent,'请先停止任务','请先停止当前计算、生成或平台任务，等待安全退出后再维护数据。');return
    try:
        with manager.maintenance() if manager else nullcontext():return operation()
    except Cancelled:return
    except Exception as exc:QMessageBox.warning(parent,'未完成维护',str(exc))

def _remove_job(host,record):
    from interaction_ui import confirm
    if not confirm(host,'彻底删除每日草稿',record['day']+' 的草稿将永久删除；已签发订单与签发时快照保留，门店默认不变。'):return
    try:
        with Session(host.work_repository.path) as session:request(host,session,'delete_job',record)
        if host.job_day==record['day']:host.saved_time='';host.job_dirty=True;host.refresh_job_label()
        host.status.setText('选定草稿已删除；当前未保存内容仍在内存，如不再需要可新建今日草稿。')
    except Cancelled:return
    except Exception as exc:QMessageBox.warning(host,'未删除草稿',str(exc))

def remove(parent,host,heads):
    result=guarded(parent,host,lambda:_remove(parent,host,heads))
    # Release the maintenance pause before opening a task that may need to run.
    if isinstance(result,tuple) and result[0]=='open_task':host.aoxiang_workflows.open_queue(result[1])
    return result

def review_blockers(parent,host,plan):
    """One complete list; never silently delete the unblocked subset."""
    d=QDialog(parent);d.setWindowTitle('删除前处理关联任务');d.resize(1100,600);v=QVBoxLayout(d)
    v.addWidget(label('当前尚未删除任何订单。处理后重新核对完整范围；不会自动跳过被拦截订单。','section'))
    blocked=plan['blockers'];action=[]
    table,model=grid(d,['订单','门店','仓','处理方式','原因'],[[b['order'],b['name'],b['warehouse'],b['action'],b['reason']] for b in blocked]);v.addWidget(table,1)
    row=QHBoxLayout();row.addWidget(button('返回重新选择',d.reject))
    local=[b for b in blocked if b['can_end']]
    def end():
        names='\n'.join(b['order'] for b in local)
        if QMessageBox.question(d,'结束关联本地准备','仅结束下列未上传本地准备，不删除订单，不操作平台：\n'+names,QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No,QMessageBox.StandardButton.No)!=QMessageBox.StandardButton.Yes:return
        try:
            manager=host.aoxiang_workflows;manager.ensure_owner(for_maintenance=True)
            for id in dict.fromkeys(b['id'] for b in local):
                group=[b for b in local if b['id']==id]
                manager.store.end_local_items(id,[b['key'] for b in group],group[0]['sequence'])
            d.accept()
        except Exception as exc:
            QMessageBox.warning(d,'请重新核对','部分本地准备可能已结束；订单尚未删除。重新核对当前状态。\n'+str(exc));d.accept()
    end_btn=button('结束关联本地准备',end);end_btn.setEnabled(bool(local));row.addWidget(end_btn)
    def open_task():
        n=table.currentIndex().row()
        if n<0:QMessageBox.information(d,'先选择任务','请点击要核对的订单任务。');return
        action.append(('open_task',blocked[model.indices[n]]['id']));d.accept()
    row.addWidget(button('查看并处理原任务',open_task));row.addWidget(button('重新核对删除范围',d.accept));v.addLayout(row)
    accepted=d.exec()==QDialog.DialogCode.Accepted
    return action[0] if action else accepted

def _remove(parent,host,heads):
    if not heads:QMessageBox.information(parent,'请选择订单','先勾选需要清理的订单。');return
    generator=getattr(host,'jst_generator',None);manager=getattr(host,'aoxiang_workflows',None)
    if host.worker or (generator and generator.worker) or (manager and manager.worker) or getattr(host,'_aoxiang_dialog',None):
        QMessageBox.information(parent,'请先停止任务','请先停止当前计算、生成或平台任务，等待安全退出后再删除，避免旧任务继续写回。');return
    try:
        while True:
            with Session(host.work_repository.path) as session:plan=request(parent,session,'delete_preview',heads)
            if not plan.get('blockers'):break
            follow=review_blockers(parent,host,plan)
            if isinstance(follow,tuple):return follow
            if not follow:return
        d=QDialog(parent);d.setWindowTitle('彻底删除订单及关联数据');d.resize(1050,650);v=QVBoxLayout(d)
        v.addWidget(label('永久删除，不进入回收站。请核对完整关联范围。','section'))
        v.addWidget(label('同一门店当天两仓共享原始计算快照，因此本次按门店当天整单删除，包含下列全部仓单、回传、付款、收货、生成结果和相关草稿数据。未选择的其他门店保留。'))
        table,model=grid(d,['日期','门店','补货父单号','包含的仓单'],[[h['day'],h['name'],h['number'],'、'.join(h['warehouses'])] for h in plan['parents']]);v.addWidget(table,1)
        if plan.get('tasks'):v.addWidget(label(f"将同时清理 {len(plan['tasks'])} 条已停止的关联任务；同批未选订单的任务保持。"))
        v.addWidget(label('磁盘原Excel、已经导出的文件、旧备份与外部平台订单不删除。其他订单或当前资料维护仍在引用的原文件保留；外部采购身份防重复记录保留，不再列为本地业务订单。'))
        ack=QCheckBox('已核对外部订单：本地删除不代表外部作废，也不恢复实际仓库存')
        ack.setVisible(bool(plan['external']));v.addWidget(ack)
        if plan['external']:v.addWidget(label('\n'.join(plan['external'])))
        scope=QCheckBox('确认删除上述完整范围（包括同店另一仓），无法在软件中恢复');v.addWidget(scope)
        row=QHBoxLayout();row.addWidget(button('取消',d.reject));go=button('永久删除上述订单',d.accept);go.setStyleSheet('color:#A52A2A;');go.setEnabled(False);row.addWidget(go);v.addLayout(row)
        def enabled():go.setEnabled(scope.isChecked() and (not plan['external'] or ack.isChecked()))
        scope.toggled.connect(enabled);ack.toggled.connect(enabled)
        if d.exec()!=QDialog.DialogCode.Accepted:return
        # Readers own no business writes; stopping them prevents stale UI/cache publication.
        readers=[host.history_reader]
        for widget in host.findChildren(type(host.followup)):
            if hasattr(widget,'reader'):readers.append(widget.reader)
        for reader in readers:reader.shutdown()
        with Session(host.work_repository.path) as session:result=request(parent,session,'delete_orders',{'plan':plan,'ack':ack.isChecked()})
    except Cancelled:return
    except Exception as exc:QMessageBox.warning(parent,'未完成删除',str(exc));return
    # Commit is complete. Refresh failure must never claim the deletion rolled back.
    try:
        codes={h['store'] for h in plan['parents'] if h['day']==host.job_day}
        if codes:
            fields=('loaded','states','results','store_presets','overrides','default_by_store','preissue_drafts','export_log')
            current={key:getattr(host,key,None) for key in fields};current['new_product_bases']=host.new_bases
            p=run(parent,maintenance.strip_current,(current,codes),'更新当前工作区',cancellable=False)
            for key in fields:setattr(host,key,p.get(key))
            host.new_bases=p['new_product_bases'];host.pending_copy=None
            host.reset_calculation_snapshot()
            host.run_codes=[s for s in host.run_codes if s not in codes]
            previous=host.initializing;host.initializing=True
            try:
                for code in sorted(codes,key=lambda s:host.store_widgets.get(s,{}).get('row',-1),reverse=True):
                    widget=host.store_widgets.pop(code,None)
                    if widget:host.store_table.removeRow(widget['row'])
                    host._store_parameter_origins.pop(code,None)
                for row,widget in enumerate(host.store_widgets.values()):widget['row']=row
            finally:host.initializing=previous
            if host.current_store in codes:host.current_store=None;host.result=None;host.dirty=True;host.navigate(3)
            host.saved_time='';host.touch();host.refresh_tables();host.selection_changed();host.update_controls()
        host.archive_checked.clear();host.refresh_archive();host.followup.query(keep_page=True)
        if manager:manager.changed.emit()
        QMessageBox.information(parent,'已彻底删除',f"已删除 {result['deleted']} 个门店当天正常单及关联业务记录。没有移入回收站。")
    except Exception as exc:
        logging.exception('删除已提交，但界面刷新失败')
        host.input_dirty=True;host.update_controls()
        QMessageBox.warning(parent,'订单已删除，页面未完成刷新','删除已提交，不要重复删除。请重新查询；当前作业请重新打开后继续。\n'+str(exc))
