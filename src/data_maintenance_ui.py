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
    v.addWidget(button('查看 / 重试组件本地清理（不连接平台）',lambda:go(lambda:retirements(host))))
    v.addWidget(button('备份与存储维护',lambda:go(host.storage_copy)))
    v.addWidget(button('关闭',d.reject));d.exec()

def retirements(host):
    from deletion_barrier import pending
    count=len(pending(host.work_repository.path))
    if not count:
        QMessageBox.information(host,'本地清理','没有待处理的组件清理；业务删除均已生效。');return
    def cleanup(progress=lambda _:None,cancel=None):
        progress('业务订单已经删除，正在重试组件本地清理；不查询平台…')
        host.aoxiang_workflows.session.start()
        host.aoxiang_workflows.session.client.flush_retirements()
        return len(pending(host.work_repository.path))
    try:
        remaining=run(host,cleanup,(),'组件本地清理',cancellable=False)
        QMessageBox.information(host,'业务已删除',f'本地关联清理待办：{remaining} 批。'+('仍有在途动作、文件占用或导出回执需核对；详见应用日志，连接可用时会重试。' if remaining else '本地关联清理已完成。'))
    except Exception as exc:QMessageBox.warning(host,'业务已删除，组件待清理','订单不会恢复；稍后可在此重试。\n'+str(exc))

def remove_job(host,record):
    return guarded(host,host,lambda:_remove_job(host,record))

def guarded(parent,host,operation):
    generator=getattr(host,'jst_generator',None);manager=getattr(host,'aoxiang_workflows',None)
    scheduled=getattr(host,'scheduled',None)
    if scheduled and (scheduled.worker or scheduled.publisher):
        QMessageBox.information(parent,'请先停止定时任务','定时补货正在执行或采用资料，请等待安全结束后再维护数据。');return
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
    result=_remove(parent,host,heads)
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
    try:
        with Session(host.work_repository.path) as session:plan=request(parent,session,'delete_preview',heads)
        d=QDialog(parent);d.setWindowTitle('彻底删除订单及关联数据');d.resize(1050,650);v=QVBoxLayout(d)
        v.addWidget(label('永久删除，不进入回收站。请核对完整关联范围。','section'))
        v.addWidget(label('只删除下方明确列出的仓单及其回传、付款、收货、生成结果和本地任务；同店未选择的另一仓保留。共享原件仍被其他仓单引用时保留。'))
        table,model=grid(d,['日期','门店','补货父单号','包含的仓单'],[[h['day'],h['name'],h['number'],'、'.join(h['warehouses'])] for h in plan['parents']]);v.addWidget(table,1)
        retained=[h['name']+'：'+'、'.join(h.get('remaining_warehouses',[])) for h in plan['parents'] if h.get('remaining_warehouses')]
        if retained:v.addWidget(label('本次保留：'+'；'.join(retained),'section'))
        if plan.get('tasks'):v.addWidget(label(f"将同时清理 {len(plan['tasks'])} 条关联任务（含运行中和待核项）；同批未选订单的任务保持。"))
        v.addWidget(label('磁盘原Excel、已经导出的文件、旧备份与外部平台订单不删除。其他订单或当前资料维护仍在引用的原文件保留；外部采购身份防重复记录保留，不再列为本地业务订单。'))
        v.addWidget(label('本地删除不撤销平台订单：已发出的请求可能继续完成。无需等待采购、付款或收货状态；同批其他订单保留。'))
        if plan['external']:v.addWidget(label('\n'.join(plan['external'])))
        scope=QCheckBox('确认仅删除上述选中仓单；未选仓保留，本地无法恢复，外部订单不撤销');v.addWidget(scope);ack=scope
        row=QHBoxLayout();row.addWidget(button('取消',d.reject));go=button('永久删除上述订单',d.accept);go.setStyleSheet('color:#A52A2A;');go.setEnabled(False);row.addWidget(go);v.addLayout(row)
        def enabled():go.setEnabled(scope.isChecked())
        scope.toggled.connect(enabled)
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
        partial=[h for h in plan.get('scopes',[]) if h['oid'] not in plan.get('full_oids',[]) and h['day']==host.job_day]
        if partial:
            fields=('results','overrides','preissue_drafts','export_log','execution_warehouses')
            while True:
                current={key:getattr(host,key,{}) for key in fields};current['new_product_bases']=host.new_bases
                original_results={h['store']:current['results'].get(h['store']) for h in partial}
                projected=run(parent,maintenance.strip_current_warehouses,(current,partial),'更新所选仓工作区',cancellable=False)
                if all(host.results.get(store) is value for store,value in original_results.items()):break
            for key in fields:
                if key in projected:setattr(host,key,projected[key])
            host.new_bases=projected.get('new_product_bases',host.new_bases)
            host.pending_copy=None
            from deletion_barrier import read_warehouse_epochs
            selected_scopes={}
            for h in partial:selected_scopes.setdefault(h['store'],set()).add(h['warehouse'])
            epochs=read_warehouse_epochs(host.work_repository.path,host.job_day,selected_scopes)
            captured={s:dict(values) for s,values in getattr(host,'warehouse_deletion_generations',{}).items()}
            for store,values in epochs.items():captured.setdefault(store,{}).update(values)
            host.warehouse_deletion_generations=captured
            if host.current_store in {h['store'] for h in partial}:host.result=host.results.get(host.current_store);host.dirty=True
            host.saved_time='';host.touch();host.refresh_tables();host.selection_changed();host.update_controls()
        from deletion_barrier import read_epochs
        selected={h['store'] for h in plan['parents'] if h['day']==host.job_day}
        epochs=read_epochs(host.work_repository.path,host.job_day,selected)
        def stale_codes():return {s for s in selected if getattr(host,'deletion_generations',{}).get(s,0)<epochs[s]}
        codes=stale_codes()
        if codes:
            fields=('loaded','states','results','store_presets','overrides','default_by_store','preissue_drafts','export_log')
            # The modal worker pumps UI callbacks. Rebase if a newer input was
            # adopted or a legitimate fresh calculation started while pruning.
            while True:
                current={key:getattr(host,key,None) for key in fields};current['new_product_bases']=host.new_bases
                p=run(parent,maintenance.strip_current,(current,codes),'更新当前工作区',cancellable=False)
                fresh=stale_codes()
                if host.loaded is current['loaded'] and fresh==codes:break
                codes=fresh
            for key in fields:
                if key=='loaded':setattr(host,key,p.get(key))
                elif key=='export_log':host.export_log=maintenance.strip_current({'export_log':host.export_log},codes)['export_log']
                else:
                    value=getattr(host,key,None)
                    if isinstance(value,dict):
                        for code in codes:value.pop(code,None)
            for code in codes:host.new_bases.pop(code,None)
            for code in codes:getattr(host,'deletion_generations',{}).pop(code,None)
            for code in codes:getattr(host,'warehouse_deletion_generations',{}).pop(code,None)
            for code in codes:getattr(host,'execution_warehouses',{}).pop(code,None)
            host.pending_copy=None
            # Keep frozen input/settings for unselected in-flight stores.
            for code in codes:getattr(host,'_calculation_settings',{}).pop(code,None)
            host._queued_retries=[s for s in getattr(host,'_queued_retries',[]) if s not in codes]
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
        deleted=set(plan.get('full_oids',plan['oids']));deleted_scopes={(h['oid'],h['warehouse']) for h in plan.get('scopes',[])}
        for widget in [host,*host.findChildren(__import__('PySide6.QtWidgets',fromlist=['QWidget']).QWidget)]:
            for attr in ('checked','archive_checked'):
                selected=getattr(widget,attr,None)
                if isinstance(selected,dict):
                    for key,head in list(selected.items()):
                        if isinstance(head,dict) and (head.get('oid') in deleted or (head.get('oid'),head.get('warehouse')) in deleted_scopes):selected.pop(key,None)
        host.refresh_archive();host.followup.query(keep_page=True)
        if manager:manager.changed.emit()
        QMessageBox.information(parent,'已彻底删除',f"已删除 {result['deleted']} 张选中仓单及关联业务记录；未选仓保留。没有移入回收站。组件本地清理已登记，将在连接可用时自动处理；不等待平台结果。")
    except Exception as exc:
        logging.exception('删除已提交，但界面刷新失败')
        host.input_dirty=True;host.update_controls()
        QMessageBox.warning(parent,'订单已删除，页面未完成刷新','删除已提交，不要重复删除。请重新查询；当前作业请重新打开后继续。\n'+str(exc))
