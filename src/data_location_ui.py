"""Business entry for a same-PC data move. Never performs a live in-place switch."""
import os
from pathlib import Path
from PySide6.QtCore import QStandardPaths,QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QDialog,QVBoxLayout,QHBoxLayout,QLineEdit,QFileDialog,QMessageBox,QCheckBox,QApplication
from ui_controls import button,label
from business_ui import run
from runtime_support import Cancelled
import data_location as location
import data_relocation as relocation

class DataLocationDialog(QDialog):
    def __init__(self,host):
        super().__init__(host);self.host=host;self.setWindowTitle('数据保存位置');self.setObjectName('dataLocationDialog');self.resize(900,610)
        self.source=Path(host.repository.folder).resolve().parent
        layout=QVBoxLayout(self);layout.addWidget(label('把软件资料保存到空间充足的磁盘','section'))
        text=label('一起搬迁订单、草稿、采集原件、门店与大仓缺货率、参数、日志、应用连接、专用浏览器资料，以及资料目录内的旧备份和待迁入材料。仅用于同一台电脑。');text.setWordWrap(True);layout.addWidget(text)
        layout.addWidget(label('当前资料位置'));self.current=QLineEdit(str(self.source));self.current.setReadOnly(True);layout.addWidget(self.current)
        row=QHBoxLayout();row.addWidget(button('打开当前资料文件夹',lambda:QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.source)))));row.addStretch();layout.addLayout(row)
        layout.addWidget(label('新资料位置（独立空文件夹，可以自动建立）'))
        row=QHBoxLayout();self.target=QLineEdit();self.target.setObjectName('dataLocationTarget');self.target.setPlaceholderText(r'例如 D:\补货助手资料');row.addWidget(self.target,1);row.addWidget(button('选择文件夹…',self.browse));layout.addLayout(row)
        text=label('先校验、复制并核对，再安全切换。完整恢复副本归档到新盘后，才清理旧位置副本；失败保留原位置和已有副本。历史订单数量、价格、版本及平台单号不重写。');text.setWordWrap(True);layout.addWidget(text)
        text=label('请先正常关闭本应用的采集专用浏览器，日常浏览器无需关闭。软件不会强行关闭浏览器或中断提交。搬迁期间重新打开软件会显示进度。','muted');text.setWordWrap(True);layout.addWidget(text)
        self.restart=QCheckBox('搬迁成功后自动重新打开补货助手；失败时仅保留结果窗口');self.restart.setChecked(True);layout.addWidget(self.restart)
        text=label('资料目录之外的既有Excel导出文件保持原位，可另外修改“导出设置”。系统盘只保留少量启动定位和恢复记录。恢复副本含本机连接及登录资料，请妥善保存。','muted');text.setWordWrap(True);layout.addWidget(text)
        self.note=label('选择新位置后开始校验；取消不会搬迁。','muted');self.note.setWordWrap(True);layout.addWidget(self.note)
        self.cleanup=button('继续归档并清理旧位置副本…',self.clean)
        prior=relocation.status() if not os.environ.get('REPLENISHMENT_DATA_DIR') else None
        self.cleanup.setEnabled(bool(prior and prior['state']=='COMPLETED' and prior['phase']=='CLEANUP_PENDING'));layout.addWidget(self.cleanup)
        if prior:self.note.setText('上次处理：'+prior['message']+'\n恢复副本：'+prior['archive'])
        self.view_progress=button('查看最近一次搬迁进度与结果',self.progress);self.view_progress.setEnabled(bool(prior));layout.addWidget(self.view_progress)
        row=QHBoxLayout();row.addWidget(button('取消',self.reject));self.move=button('校验位置并搬迁全部资料…',self.prepare,True);self.move.setObjectName('dataLocationMove');row.addWidget(self.move);layout.addLayout(row)
        if os.environ.get('REPLENISHMENT_DATA_DIR'):
            self.move.setEnabled(False);self.note.setText('当前由启动参数指定独立数据目录；本入口不会搬迁其上级文件夹。普通启动时可在这里更改保存位置。')
        from table_readability import fit_window
        fit_window(self,900,610)
        from data_location_pause import AutomaticPause
        self.pause=AutomaticPause(host)

    def done(self,code):
        if not getattr(self.host,'_data_location_request',None):self.pause.restore()
        super().done(code)

    def browse(self):
        selected=QFileDialog.getExistingDirectory(self,'选择新的独立资料文件夹',self.target.text().strip())
        if selected:
            p=Path(selected)
            self.target.setText(str(p/'补货助手资料' if p==Path(p.anchor) else p))

    def prepare(self):
        plan=None
        try:
            from data_location_pause import finish_login_checks
            startup=getattr(self.host,'browser_startup',None)
            workers=list(startup.workers.values()) if startup else []
            if workers:run(self,finish_login_checks,(workers,),'安全结束现有登录检查')
            from order_migration_ui import idle
            idle(self.host)
            if getattr(self.host,'_order_import_request',None) or getattr(self.host,'_factory_reset_request',None):raise ValueError('已有其他资料操作在等待退出，请先完成原操作')
            if not self.target.text().strip():raise ValueError('请先选择新资料文件夹')
            plan=run(self,relocation.prepare,(self.source,self.target.text().strip()),'核对新位置和资料范围')
            text=(f'当前：{plan["source"]}\n新位置：{plan["target"]}\n'
                  f'约{plan["estimated_files"]}份文件，{plan["estimated_bytes"]/1024**3:.2f} GiB。\n'
                  f'完整恢复副本：{plan["archive"]}\n\n'
                  '确认后安全退出并搬迁，保留旧文件路径兼容；校验与归档成功后释放旧副本占用。\n'
                  '不会撤销、重发或重新生成任何平台订单。请等待搬迁结束后使用新位置。')
            if QMessageBox.question(self,'确认搬迁全部本机资料',text,QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No,QMessageBox.StandardButton.No)!=QMessageBox.StandardButton.Yes:
                relocation.cancel(plan);return
            if not self.host.leave_current('更改数据位置'):
                relocation.cancel(plan);return
            relocation.record(plan,'WAITING_EXIT',message='已确认，等待本应用后台任务安全退出')
            from data_location_progress import launch
            import sys
            qa=None
            if '--data-location-smoke' in sys.argv:
                index=sys.argv.index('--data-location-smoke');qa=(Path(sys.argv[index+1]),sys.argv[index+2])
            run(self,launch,(plan,qa),'打开搬迁进度窗口',cancellable=False)
            self.host._data_location_request=plan;self.host._data_location_restart=self.restart.isChecked();self.host._discard_close=True
            # Unsaved-work handling ran above while normal error UI was available.
            from application_runtime import current
            runtime=current()
            if runtime:runtime.storage_request=plan;runtime.shutdown_errors=0;runtime.first_shutdown_exception=None;runtime.transition('WAITING_EXIT')
            self.accept()
            self.host.setEnabled(False)
            if not self.host.close():self.host.status.setText('资料搬迁等待安全退出；如取消退出，请在数据保存位置查看或取消原搬迁。')
        except Cancelled:
            if plan:relocation.cancel(plan)
        except Exception as exc:
            if plan:
                if getattr(self.host,'_data_location_request',None) is plan:
                    from migration_status import shutdown_failed
                    shutdown_failed(self.host,exc)
                else:relocation.cancel(plan)
            QMessageBox.warning(self,'数据位置未更改',str(exc))

    def progress(self):
        value=relocation.status()
        if value:
            from data_location_progress import dialog
            dialog(self,value['nonce']).exec()

    def clean(self):
        value=relocation.status()
        if not value:return
        if QMessageBox.question(self,'归档并清理旧位置副本','当前使用：'+value['target']+'\n将校验恢复归档后清理已记录旧副本：'+value['backup']+'\n恢复副本保留：'+value['archive'],QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No,QMessageBox.StandardButton.No)!=QMessageBox.StandardButton.Yes:return
        try:
            result=run(self,relocation.retry_cleanup,(),'归档并清理旧位置副本')
            self.note.setText(result['message']+'\n恢复副本：'+result['archive']);self.cleanup.setEnabled(result['phase']=='CLEANUP_PENDING')
        except Cancelled:self.note.setText('已在安全边界取消；完整恢复副本或旧位置副本保留，可再次继续。')
        except Exception as exc:QMessageBox.warning(self,'旧副本尚未清理',str(exc))

def show(host):DataLocationDialog(host).exec()

def reopen(runtime):
    import subprocess,sys
    result=getattr(runtime,'location_result',None)
    if not result or result['state']!='COMPLETED' or not getattr(runtime,'storage_restart',False):return
    command=[sys.executable] if getattr(sys,'frozen',False) else [sys.executable,str(Path(__file__).with_name('app.py'))]
    if '--data-location-smoke' in sys.argv:
        i=sys.argv.index('--data-location-smoke');command+=['--data-location-smoke',sys.argv[i+1],'reopen']
    try:subprocess.Popen(command,cwd=location.control(),env=dict(os.environ),creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    except Exception as exc:
        from stockout_common import atomic_json
        prior=relocation.status();prior['reopen_error']=str(exc);prior['message']+='；自动重开未完成，请手动打开'
        atomic_json(location.journal_file(),prior)
