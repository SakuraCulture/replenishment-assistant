"""Explicit spreadsheet mapping and preview for reusable monthly focus membership."""
from PySide6.QtWidgets import QDialog,QVBoxLayout,QHBoxLayout,QComboBox,QLineEdit,QTableWidget,QTableWidgetItem,QMessageBox
from ui_controls import label,button
import new_product_focus as focus


class FocusMapping(QDialog):
    def __init__(self,parent,info,month):
        super().__init__(parent);self.setObjectName('focusMapping');self.setWindowTitle('导入新品明细 · 指定关注字段');self.resize(650,380);v=QVBoxLayout(self);self.info=info
        v.addWidget(label('按商品编码精确匹配本店新品候选；关注只决定导出分组，不会自动选品或变成必卖。','muted'))
        self.month=QLineEdit(month);v.addWidget(label('适用月份（YYYY-MM）'));v.addWidget(self.month)
        self.sheet=QComboBox();self.sheet.addItems(info['sheets']);v.addWidget(label('工作表（首行为表头）'));v.addWidget(self.sheet)
        self.sku=QComboBox();self.focus=QComboBox()
        for title,widget in [('商品编码字段',self.sku),('关注字段',self.focus)]:v.addWidget(label(title));v.addWidget(widget)
        v.addWidget(label('关注：关注/已关注/是/1/true/√/✓；未关注：空白/未关注/不关注/否/0/false。其他值报错。','muted'))
        self.sheet.currentTextChanged.connect(self.refresh);self.refresh();row=QHBoxLayout();row.addWidget(button('读取并预览',self.accept,True));row.addWidget(button('取消',self.reject));v.addLayout(row)
    def refresh(self,*_):
        headers=self.info['sheets'][self.sheet.currentText()]
        for widget,preferred in [(self.sku,'商品编码'),(self.focus,'关注')]:
            widget.clear();widget.addItems(headers)
            if preferred in headers:widget.setCurrentText(preferred)


def confirm_focus(parent,data,old):
    box=QMessageBox(parent);box.setObjectName('focusConfirm');box.setWindowTitle('确认使用新品关注清单');box.setText(f"适用月份：{data['month']}\n{data['name']}\n共 {len(data['items'])} 个商品，关注 {len(focus.members(data))} 个。\n用于本月/往期新品分组；未在本店新品候选中的商品不会新增。\n应用后保存为后续作业默认清单，并使当前签发分组需要重新确认；历史修订保留原清单。")
    box.setDetailedText('来源：'+data['source_path']+'\n文件校验：'+data['source_sha256']+'\n原清单：'+(old['name'] if old else '未设置')+'\n关注商品：\n'+'\n'.join(x['sku']+' '+x['name'] for x in data['items'] if x['focused']))
    cancel=box.addButton('取消',QMessageBox.ButtonRole.RejectRole);accept=box.addButton('使用并保存清单',QMessageBox.ButtonRole.AcceptRole);box.setDefaultButton(cancel);box.exec();return box.clickedButton() is accept
