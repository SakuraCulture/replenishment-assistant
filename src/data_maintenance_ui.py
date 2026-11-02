"""One permanent-delete preview; no new recycle-bin destinations."""
from PySide6.QtWidgets import QDialog,QVBoxLayout,QHBoxLayout,QCheckBox,QMessageBox
from ui_controls import label,button
from business_ui import run,grid
from runtime_support import Cancelled
from contextlib import nullcontext
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
        run(host,maintenance.delete_job,(host.work_repository.path,record['day'],record['revision']),'删除每日草稿')
        if host.job_day==record['day']:host.saved_time='';host.job_dirty=True;host.refresh_job_label()
        host.status.setText('选定草稿已删除；当前未保存内容仍在内存，如不再需要可新建今日草稿。')
    except Cancelled:return
    except Exception as exc:QMessageBox.warning(host,'未删除草稿',str(exc))

def remove(parent,host,heads):
    return guarded(parent,host,lambda:_remove(parent,host,heads))

def _remove(parent,host,heads):
    if not heads:QMessageBox.information(parent,'请选择订单','先勾选需要清理的订单。');return
    generator=getattr(host,'jst_generator',None);manager=getattr(host,'aoxiang_workflows',None)
    if host.worker or (generator and generator.worker) or (manager and manager.worker) or getattr(host,'_aoxiang_dialog',None):
        QMessageBox.information(parent,'请先停止任务','请先停止当前计算、生成或平台任务，等待安全退出后再删除，避免旧任务继续写回。');return
    try:
        plan=run(parent,maintenance.prepare,(host.work_repository.path,heads),'核对彻底删除范围')
        d=QDialog(parent);d.setWindowTitle('彻底删除订单及关联数据');d.resize(1050,650);v=QVBoxLayout(d)
        v.addWidget(label('永久删除，不进入回收站。请核对完整关联范围。','section'))
        v.addWidget(label('同一门店当天两仓共享原始计算快照，因此本次按门店当天整单删除，包含下列全部仓单、回传、付款、收货、生成结果和相关草稿数据。未选择的其他门店保留。'))
        table,model=grid(d,['日期','门店','补货父单号','包含的仓单'],[[h['day'],h['name'],h['number'],'、'.join(h['warehouses'])] for h in plan['parents']]);v.addWidget(table,1)
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
        result=run(parent,maintenance.execute,(host.work_repository.path,plan,ack.isChecked()),'彻底删除关联数据')
        codes={h['store'] for h in plan['parents'] if h['day']==host.job_day}
        if codes:
            p=maintenance.strip_work(host.snapshot(),codes)
            host._calculation_settings={};host._calculation_input=None;host._queued_retries=[]
            host.restore_job({'day':host.job_day,'saved':'','payload':p},owned=True);host.touch()
        host.archive_checked.clear();host.refresh_archive();host.followup.query(keep_page=True)
        QMessageBox.information(parent,'已彻底删除',f"已删除 {result['deleted']} 个门店当天正常单及关联业务记录。没有移入回收站。")
    except Cancelled:return
    except Exception as exc:QMessageBox.warning(parent,'未完成删除',str(exc))
