"""On-demand export receipts and explicitly selected original-revision recovery."""
from pathlib import Path
from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QComboBox, QTableWidget, QTableWidgetItem
from ui_controls import button, label
from business_ui import run
from runtime_support import Cancelled
import export_receipts
import export_recovery


def show(parent, database):
    dialog = QDialog(parent);dialog.setWindowTitle('导出任务与恢复');dialog.resize(1100, 700)
    from table_readability import fit_window
    fit_window(dialog)
    layout = QVBoxLayout(dialog)
    layout.addWidget(label('查看签发状态和逐文件结果；恢复只读取原修订，不重新签发。', 'section'))
    choices = QComboBox();layout.addWidget(choices)
    tasks = export_receipts.recent(database)
    for task in tasks:
        choices.addItem(f'{task.get("created", "")} · {task.get("signature", "")} · {task["state"]}', task['id'])
    note = label('仅显示最近 100 个本地导出任务；旧版本未记录的任务请从订单跟踪按原修订导出。')
    layout.addWidget(note)
    table = QTableWidget(0, 5);table.setHorizontalHeaderLabels(['恢复', '文件', '原单 / 修订', '处理方式', '原因与下一步'])
    table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    for i, width in enumerate((60, 220, 260, 150, 410)):table.setColumnWidth(i, width)
    layout.addWidget(table, 1)
    state = {'review': None}
    def refresh():
        if not choices.currentData():return
        try:
            review = run(dialog, export_recovery.inspect, (database, choices.currentData()), '核对原修订及已导出文件')
            state['review'] = review;table.setRowCount(len(review['items']))
            for n, item in enumerate(review['items']):
                check = QTableWidgetItem();check.setFlags(Qt.ItemFlag.ItemIsEnabled | (Qt.ItemFlag.ItemIsUserCheckable if item['action'] in ('export', 'record') else Qt.ItemFlag.NoItemFlags))
                check.setCheckState(Qt.CheckState.Checked if item['action'] in ('export', 'record') else Qt.CheckState.Unchecked);table.setItem(n, 0, check)
                values = [Path(item['path']).name, '\n'.join(h['number'] + f' V{h["revision"]:03d}' for h in item['heads']),
                          {'keep': '文件已保留', 'export': '补导出至新目录', 'record': '只补导出记录', 'blocked': '先核对原单'}[item['action']], item['reason']]
                for c, value in enumerate(values, 1):
                    cell = QTableWidgetItem(value);cell.setToolTip(value);table.setItem(n, c, cell)
            table.resizeRowsToContents()
            note.setText(review['receipt'].get('signature', '') + '\n' + '\n'.join(review['receipt'].get('warnings', [])))
        except Cancelled:pass
        except Exception as exc:note.setText(str(exc));state['review'] = None
    def recover():
        if not state['review']:return
        selected = [n for n in range(table.rowCount()) if table.item(n, 0).checkState() == Qt.CheckState.Checked]
        if not selected:note.setText('请勾选需要恢复的文件。');return
        import export_locations
        folder = export_locations.directory(dialog, '选择恢复位置（自动新建目录，保留原文件）')
        if not folder:return
        try:
            result = run(dialog, export_recovery.recover, (database, state['review'], selected, folder), '按原修订恢复勾选导出')
            refresh();note.setText('已处理勾选项。' + '\n'.join(result['warnings']))
            from readable_ui import file_results
            file_results(dialog, result['paths'], '恢复结果 · 原签发不变')
        except Cancelled:note.setText('已安全停止，请重新核对导出任务中的已完成项。')
        except Exception as exc:note.setText(str(exc))
    row = QHBoxLayout();row.addWidget(button('核对所选任务', refresh));row.addWidget(button('按勾选项恢复', recover, True))
    row.addWidget(button('打开回执目录', lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(export_receipts.directory(database))))))
    row.addWidget(button('关闭', dialog.accept));layout.addLayout(row)
    choices.currentIndexChanged.connect(lambda _:state.update(review=None))
    dialog.exec()
