"""Explicit whole-order export/restore entry inside order tracking."""
from pathlib import Path
from PySide6.QtWidgets import QDialog,QVBoxLayout,QMessageBox,QFileDialog
from ui_controls import label,button
from business_ui import run
from runtime_support import Cancelled

def idle(host):
    for name in ('stockout','warehouse_stockout','jst_generator','scheduled','browser_startup'):
        owner=getattr(host,name,None)
        if owner and (getattr(owner,'worker',None) or getattr(owner,'publisher',None) or getattr(owner,'queued',None)):raise ValueError('请等待计算、分析、连接核验和后台作业安全结束，再进行完整订单迁移')
    if getattr(host,'worker',None) or getattr(host,'_aoxiang_dialog',None):raise ValueError('请先安全结束当前业务操作，再迁移')
    sessions=getattr(host,'catalog_sessions',None)
    if sessions and sessions.session.lease is not None:raise ValueError('请先关闭聚水潭采集窗口，等待原任务安全结束')

def open_migration(parent,host):
    from data_maintenance_ui import guarded
    def show():
        idle(host);d=QDialog(parent);d.setWindowTitle('完整订单迁移与参数导出');d.resize(760,400);v=QVBoxLayout(d)
        v.addWidget(label('迁移全部本地订单，保留完整关联','section'))
        text=label('不受当前日期、筛选或勾选限制。包含订单全部修订、草稿、回传、库存分配、付款、实收、平台单号、防重事实、参数及留存原件。\n浏览器登录、机器连接配置、AI密钥和两个独立缺货分析板块不随包迁移。');text.setWordWrap(True);v.addWidget(text)
        def save():
            from order_migration import export
            path,_=QFileDialog.getSaveFileName(d,'导出完整订单迁移包','补货助手_完整订单迁移.zip','Zip (*.zip)')
            if not path:return
            try:
                idle(host)
                def export_with_snapshot(root,target,progress,cancel):
                    sessions=getattr(host,'catalog_sessions',None);client=sessions.session.client if sessions else None
                    snapshot=client.request('sync.migration_snapshot') if client else None
                    return export(root,target,progress,cancel,catalog_state=snapshot)
                result=run(d,export_with_snapshot,(host.repository.folder,path),'生成并校验完整订单迁移包')
                missing=len(result['manifest']['missing_external']);QMessageBox.information(d,'迁移包已导出',result['path']+f'\n未找到的外部路径 {missing} 项，清单已保留；数据库内已留存原件随包保存。\n校验摘要：'+result['sha256'])
            except Cancelled:return
            except Exception as exc:QMessageBox.warning(d,'迁移包未导出',str(exc))
        def restore():
            from order_migration import prepare,discard
            path,_=QFileDialog.getOpenFileName(d,'选择完整订单迁移包','','Zip (*.zip)')
            if not path:return
            plan=None
            try:
                idle(host);plan=run(d,prepare,(host.repository.folder,path),'完整性校验与导入预览')
                m=plan['manifest'];counts=m['counts']
                text=(f'来源版本：{m["app_version"]}，导出时间：{m["created"]}\n订单父单 {counts.get("normal_orders",0)}，签发修订 {counts.get("normal_revisions",0)}，草稿 {counts.get("jobs",0)}，库内原件 {counts.get("source_files",0)}。\n'
                      f'将完整替换本机订单、草稿、关联平台记录及门店参数/清单，不与现有订单混合。\n本机完整备份：{plan["backup"]}\n'
                      '本机浏览器连接、登录及独立分析资料保留。平台订单不会撤销；迁入作业和计划暂停，先核验原平台结果后逐项继续。\n未保存的界面内容不保存。确认后安全退出，迁入完成后重新打开软件。')
                if QMessageBox.question(d,'核对完整恢复范围',text,QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No,QMessageBox.StandardButton.No)!=QMessageBox.StandardButton.Yes:discard(plan);return
                # False here also means a normal asynchronous safe shutdown.
                # Keep the accepted request until app.exec() ends and app.py
                # consumes it; do not mistake a pending close for cancellation.
                host._order_import_request=plan;host._discard_close=True;d.accept()
                host.setEnabled(False)
                if not host.close():host.status.setText('迁入已确认，正在等待后台连接安全退出；退出后自动完成迁入，请勿重复操作。')
            except Cancelled:
                if plan:discard(plan)
            except Exception as exc:
                if plan:
                    if getattr(host,'_order_import_request',None) is plan:
                        host._order_import_request=None;host._discard_close=False;host.setEnabled(True)
                    discard(plan)
                QMessageBox.warning(d,'订单未迁入',str(exc))
        def parameters():
            from parameter_snapshot import export
            path,_=QFileDialog.getSaveFileName(d,'导出全部已保存参数','补货助手_全部门店参数.xlsx','Excel (*.xlsx)')
            if path:
                try:result=run(d,export,(host.work_repository,path),'导出完整门店参数');QMessageBox.information(d,'参数已导出',result)
                except Cancelled:return
                except Exception as exc:QMessageBox.warning(d,'参数未导出',str(exc))
        v.addWidget(button('导出完整订单迁移包…',save,True));v.addWidget(button('校验并导入完整订单迁移包…',restore));v.addWidget(button('导出全部门店参数与预案…',parameters));v.addWidget(button('关闭',d.reject));d.exec()
    return guarded(parent,host,show)
