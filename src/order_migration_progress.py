"""Independent taskbar window that observes one order migration receipt."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid
from datetime import timedelta

from application_processes import watch_parent
from data_location_status import read
from migration_paths import io_path
from migration_status import folder
from runtime_support import checkpoint


PHASES = {
    'PREPARED': '等待工作台安全退出',
    'WAITING_EXIT': '等待工作台和后台连接安全退出',
    'UI_TEARDOWN': '关闭工作台',
    'VALIDATING': '复核迁入材料',
    'WAITING_LOCKS': '核对资料占用',
    'COPYING': '复制本机资料',
    'MERGING': '合并订单并校验',
    'FINAL_CHECK': '校验写入后的独立副本',
    'REPLACING': '切换正式订单库',
    'CLEANUP_PENDING': '已写入，临时资料待清理',
}


def status_path(root, operation):
    if not re.fullmatch('[a-f0-9]{32}', operation):
        raise ValueError('迁入操作身份无效')
    return folder(root) / (operation + '.status.json')


def read_status(root, operation):
    # A status reader must allow the writer to replace the same file on Windows.
    value = read(io_path(status_path(root, operation)))
    recorded_root = value.get('root') if isinstance(value, dict) else None
    if (not isinstance(recorded_root, str) or value.get('nonce') != operation
            or Path(recorded_root).resolve() != Path(root).resolve()):
        raise ValueError('迁入记录与本次作业不一致')
    return value


def presentation(value, writer_state):
    state = value.get('state', '')
    phase = value.get('phase', state)
    step = PHASES.get(phase, phase or '等待读取迁入进度')
    if state == 'FAILED':
        error = value.get('error') or {}
        detail = error.get('message') or '请查看本次迁入的诊断记录。'
        if error.get('type'):
            detail = error['type'] + '\n' + detail
        return '订单迁入失败', '失败阶段 ' + step, detail, True
    if state == 'COMPLETED':
        result = value.get('result') or {}
        counts = result.get('counts')
        detail = '正式订单库已写入。'
        if result.get('mode') == 'merge' and counts is not None:
            detail = ('新增 {added} 单 · 重复 {duplicate} 单 · 冲突保留本机 {conflict} 单 · '
                      '本机已删除 {deleted} 单').format(**counts)
        if value.get('backup'):
            detail += '\n原资料备份 ' + value['backup']
        return '订单迁入完成', PHASES.get(phase, '正式订单库已写入'), detail, True
    if state == 'CANCELLED':
        return '订单迁入已取消', '本次作业已结束', '', True
    if writer_state == 'EXITED':
        return ('迁入进程提前退出，结果待核对', '最后记录阶段 ' + step,
                '请重新打开补货助手查看迁入结果，核实前请勿重复导入。', True)
    if writer_state == 'UNKNOWN':
        return '暂无法核验后台进程', step, '正在等待本次作业状态。', False
    return '订单迁入正在进行', '当前阶段 ' + step, '', False


def launch(plan, progress=lambda _: None, cancel=None):
    """Open the viewer before the existing, guarded write handoff."""
    checkpoint(cancel)
    root, operation = Path(plan['root']), plan['nonce']
    if read_status(root, operation)['state'] != 'PREPARED':
        raise ValueError('本次迁入状态已变化，请重新核对')
    progress('正在打开独立迁入进度窗口…')
    token = uuid.uuid4().hex
    status_folder = folder(root)
    ready = status_folder / (token + '.progress-ready.json')
    log = status_folder / (operation + '.progress.log')
    command = ([sys.executable, '--order-migration-progress'] if getattr(sys, 'frozen', False)
               else [sys.executable, str(Path(__file__).resolve())])
    command += ['--root', str(root), '--operation', operation, '--writer', str(os.getpid()),
                '--ready-token', token]
    with log.open('ab') as stderr:
        child = subprocess.Popen(command, cwd=status_folder, stdout=subprocess.DEVNULL, stderr=stderr,
                                 creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    deadline = time.monotonic() + 12
    try:
        while not ready.is_file():
            if child.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError('迁入进度窗口未能打开，工作台仍保留。诊断记录 ' + str(log))
            checkpoint(cancel)
            time.sleep(.05)
        value = json.loads(ready.read_text('utf-8'))
        if (value != {'operation': operation, 'token': token, 'writer': os.getpid()}
                or child.poll() is not None):
            raise RuntimeError('迁入进度窗口未就绪，请重新核对原迁移包')
    finally:
        ready.unlink(missing_ok=True)


def dialog(root, operation, writer_state):
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtWidgets import (QApplication, QDialog, QHBoxLayout, QLabel, QPlainTextEdit,
                                   QProgressBar, QPushButton, QVBoxLayout)
    from table_readability import fit_window

    def text():
        label = QLabel()
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        return label

    class View(QDialog):
        def __init__(self):
            super().__init__(None, Qt.WindowType.Window | Qt.WindowType.WindowMinimizeButtonHint |
                             Qt.WindowType.WindowCloseButtonHint)
            self.setWindowTitle('补货助手 · 订单迁入进度')
            self.setObjectName('orderMigrationProgress')
            self.finished = False
            self.value = {}
            self.started = time.monotonic()
            layout = QVBoxLayout(self)
            self.title = text()
            self.title.setStyleSheet('font-size:17pt;font-weight:600')
            layout.addWidget(self.title)
            self.stage = text()
            layout.addWidget(self.stage)
            self.progress = QProgressBar()
            self.progress.setRange(0, 0)
            layout.addWidget(self.progress)
            self.activity = text()
            layout.addWidget(self.activity)
            self.details = QPlainTextEdit(readOnly=True)
            layout.addWidget(self.details, 1)
            self.path = text()
            self.path.setText('诊断记录 ' + str(status_path(root, operation)))
            self.path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            layout.addWidget(self.path)
            buttons = QHBoxLayout()
            self.copy_button = QPushButton('复制诊断')
            self.copy_button.clicked.connect(self.copy_diagnostic)
            buttons.addWidget(self.copy_button)
            buttons.addStretch()
            self.close_button = QPushButton('关闭')
            self.close_button.clicked.connect(self.accept)
            buttons.addWidget(self.close_button)
            layout.addLayout(buttons)
            self.timer = QTimer(self, interval=500)
            self.timer.timeout.connect(self.refresh)
            self.timer.start()
            self.refresh()
            fit_window(self, 650, 355)

        def copy_diagnostic(self):
            state = self.value.get('state', '')
            phase = self.value.get('phase', state)
            QApplication.clipboard().setText('\n'.join((self.title.text(), self.stage.text(),
                f'{state} / {phase}', self.activity.text(), self.details.toPlainText(), self.path.text())))

        def refresh(self):
            live = writer_state()
            stale = False
            try:
                self.value = read_status(root, operation)
            except (OSError, ValueError, KeyError) as error:
                stale = True
                read_error = str(error)
            title, stage, detail, finished = presentation(self.value, live)
            self.finished = finished
            self.title.setText(title)
            self.stage.setText(stage)
            self.progress.setVisible(not finished)
            if finished:
                self.timer.stop()
                activity = '最后回执 ' + self.value.get('at', '')
            else:
                elapsed = str(timedelta(seconds=int(time.monotonic() - self.started)))
                activity = ('后台进程运行中' if live == 'RUNNING' else '后台进程状态待核对')
                activity += ' · 已用时 ' + elapsed
                current = self.value.get('progress', {})
                if current.get('label'):
                    activity += '\n' + current['label']
                    if 'current' in current:
                        activity += f" · {current['current']:,} / {current['total']:,}"
            if stale:
                detail = '\n'.join(filter(None, (detail, '暂未读取到最新迁入记录。', read_error)))
            self.activity.setText(activity)
            self.details.setVisible(bool(detail))
            if self.details.toPlainText() != detail:
                self.details.setPlainText(detail)
            diagnostic = finished and self.value.get('state') not in ('COMPLETED', 'CANCELLED')
            self.path.setVisible(diagnostic)
            self.copy_button.setVisible(diagnostic)
            self.close_button.setEnabled(finished)

        def reject(self):
            if self.finished:
                super().reject()

        def closeEvent(self, event):
            if self.finished:
                super().closeEvent(event)
            else:
                event.ignore()

    return View()


def main(args=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--operation', required=True)
    parser.add_argument('--writer', type=int, required=True)
    parser.add_argument('--ready-token', required=True)
    options = parser.parse_args(args)
    if options.writer <= 0 or not re.fullmatch('[a-f0-9]{32}', options.ready_token):
        raise ValueError('迁入进度窗口身份无效')
    status_path(options.root, options.operation)
    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication
    from stockout_common import atomic_json
    from ui_controls import STYLE
    with watch_parent(options.writer) as writer_state:
        app = QApplication(['订单迁入进度'])
        app.setApplicationName('补货助手订单迁入进度')
        app.setWindowIcon(QIcon(str(Path(__file__).parent / 'app.ico')))
        app.setStyle('Fusion')
        app.setStyleSheet(STYLE)
        view = dialog(options.root, options.operation, writer_state)
        view.show()

        def ready():
            if view.isVisible() and view.value.get('state') == 'PREPARED' and writer_state() == 'RUNNING':
                atomic_json(folder(options.root) / (options.ready_token + '.progress-ready.json'),
                            {'operation': options.operation, 'token': options.ready_token, 'writer': options.writer})
            elif not view.finished:
                # Failed startup must never let the writer start an invisible handoff.
                app.exit(1)

        QTimer.singleShot(0, view, ready)
        return app.exec()


if __name__ == '__main__':
    raise SystemExit(main())
