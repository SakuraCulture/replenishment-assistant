"""Small, explicit navigation help; no automatic imports or business edits."""
from PySide6.QtGui import QShortcut,QKeySequence
from PySide6.QtWidgets import QDialog,QVBoxLayout,QTextBrowser
from ui_controls import button

GUIDE='''第一次做补货
1. 工作页 → 准备数据：拖入门店补货建议（可以多个文件、一份多店）。导入框可下载最低标准模板，编码请保留为文本。
2. 分别导入河北仓、山东仓商品资料，外部指定仓库。单仓只需导入该仓。旧版混合资料可使用兼容入口。资料维护后可多次复用。
3. 点击检查文件。先核对失败文件、字段和重复冲突；冲突可筛选后按来源批量提出选择，默认不替你决定。
4. 选择门店，确认主从仓、预算、参数。不限预算可先计算；启用预算必须先导入必卖清单。无分类沿用快销，销量库存和补货量均为库存单位。
5. 点击开始计算。空位会继续下一店；停止只停止后续任务，正在运行的门店安全完成。参数变动后需重算。
6. 看动销补货、新品推荐和待核项；导出加盟商确认单前先维护新品关注清单，或明确本月无关注新品。确认后按日归档，同日覆盖需再次确认。
7. 历史页按日期、门店和商品查询。先打开单据，再点选商品查看当时参数、销量库存、商品资料。表单导出用于追溯，不是门店确认单；回传数量独立保存，不覆盖签发建议。

快捷键：Ctrl+1 首页；Ctrl+2 工作页；Ctrl+3 历史；Ctrl+S 保存草稿；Ctrl+Enter 开始计算；F1 本帮助。保存和计算只在对应按钮可用时触发，原有确认仍保留。
'''

def install(win,home_layout):
    def guide():
        d=QDialog(win);d.setWindowTitle('首次使用 / 快捷键');d.resize(850,650);v=QVBoxLayout(d);text=QTextBrowser();text.setPlainText(GUIDE);v.addWidget(text);v.addWidget(button('进入准备数据',lambda:(d.accept(),win.show_section(1),win.navigate(0))));v.addWidget(button('关闭',d.accept));d.exec()
    home_layout.insertWidget(2,button('第一次使用 / 操作指南（F1）',guide))
    actions=[('Ctrl+1',lambda:win.show_section(0)),('Ctrl+2',lambda:win.show_section(1)),('Ctrl+3',lambda:win.show_section(2)),('Ctrl+S',win.save_job_btn.click),('Ctrl+Return',win.calculate_btn.click),('F1',guide)]
    win._navigation_shortcuts=[]
    for key,action in actions:
        shortcut=QShortcut(QKeySequence(key),win);shortcut.activated.connect(action);win._navigation_shortcuts.append(shortcut)
