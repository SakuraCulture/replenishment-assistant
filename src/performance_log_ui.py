"""One business-facing export button; no database, profiles or full app logs."""
from datetime import datetime
from PySide6.QtWidgets import QDialog,QVBoxLayout,QMessageBox,QFileDialog
from ui_controls import label,button


def show(host):
    import performance_log
    monitor=performance_log._current
    d=QDialog(host);d.setWindowTitle('轻量性能日志');d.setObjectName('performanceLog');d.resize(620,310)
    v=QVBoxLayout(d)
    v.addWidget(label('资源与阶段耗时 · 自动记录近24小时','section'))
    text=label('每5秒记录主程序及直接持有的作业子进程资源，记录阶段耗时和界面响应延迟。日志总量上限16MiB，过期自动清理。\n不包含商品、单据、金额、文件路径或登录资料；日志失败不阻塞作业。\n出现速度慢或异常后，导出ZIP发给排查人员即可。')
    text.setWordWrap(True);v.addWidget(text)
    running=monitor is not None and monitor.thread is not None and monitor.thread.is_alive() and not monitor.stopped.is_set()
    status=label('记录中' if running else '当前会话未开启记录；仍可导出已有日志','muted');v.addWidget(status)
    def export_now():
        import export_locations
        from business_ui import run
        from runtime_support import Cancelled
        if monitor is None:
            QMessageBox.information(d,'性能日志未开启','请正常重启软件后再记录。');return
        name='补货助手_轻量性能日志_'+datetime.now().strftime('%Y%m%d_%H%M%S_%f')+'.zip'
        path,_=export_locations.save_file(d,'导出近24小时性能日志',name,'ZIP文件 (*.zip)',options=QFileDialog.Option.DontConfirmOverwrite)
        if not path:return
        if not path.lower().endswith('.zip'):path+='.zip'
        try:
            result=run(d,performance_log.export,(monitor.folder,path),'导出轻量性能日志')
            status.setText('已导出：'+result)
            QMessageBox.information(d,'性能日志已导出','请将这个ZIP发给排查人员：\n'+result)
        except Cancelled:status.setText('已取消导出；原性能日志保留')
        except Exception as exc:QMessageBox.warning(d,'性能日志未导出',str(exc))
    export_button=button('导出近24小时性能日志',export_now,True);export_button.setObjectName('exportPerformanceLog');v.addWidget(export_button)
    v.addStretch();v.addWidget(button('关闭',d.accept));d.exec();d.deleteLater()
