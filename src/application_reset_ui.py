"""Explicit whole-data reset; runs only after safe application shutdown."""
from PySide6.QtWidgets import QApplication, QMessageBox


def request_reset(host):
    from application_reset import plan
    root = getattr(QApplication.instance(), 'data_root', None)
    if root is None:
        QMessageBox.warning(host, '不能初始化', '当前启动方式未登记资料目录，请从主程序重新打开。'); return
    try: value = plan(root)
    except Exception as exc:
        QMessageBox.warning(host, '不能初始化', str(exc)); return
    text = ('将把本应用的全部本机资料恢复为程序初始默认：\n'
            '订单、草稿、采集原件、作业及日志、缺货率分析、门店参数、必卖清单、应用连接与自动作业配置。\n\n'
            '平台上已有采购单不撤销，浏览器本身的登录资料不删除；本机平台关联也会移入备份，初始化后不要把旧采购重复导入。\n\n'
            f'资料目录：{value["root"]}\n完整备份：{value["backup"]}\n\n'
            '确认后安全退出程序，移动整个资料目录作为备份，再建立空白目录。重新打开程序后生效。')
    box = QMessageBox(QMessageBox.Icon.Warning, '全部初始化（开发测试）', text, parent=host)
    confirm = box.addButton('备份并全部初始化', QMessageBox.ButtonRole.DestructiveRole)
    cancel = box.addButton('取消', QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(cancel); box.exec()
    if box.clickedButton() != confirm: return
    old_discard = getattr(host, '_discard_close', False)
    host._discard_close = True; host._factory_reset_request = value
    if not host.close():
        host._factory_reset_request = None; host._discard_close = old_discard
        QMessageBox.information(host, '正在安全结束作业', '本次尚未初始化。请等当前作业安全结束、关闭采集窗口后，再点击全部初始化；不会强制中断提交。')
