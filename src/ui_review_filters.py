"""Small independent business filters; labels never act as numeric data."""
from PySide6.QtCore import Signal,QSignalBlocker
from PySide6.QtWidgets import QWidget,QHBoxLayout,QVBoxLayout,QComboBox
from ui_controls import label,button
from review_filters import GROUPS,KINDS

class CountCombo(QComboBox):
    def currentText(self):return self.currentData() or super().currentText()
    def setCurrentText(self,text):
        index=self.findData(text)
        if index>=0:self.setCurrentIndex(index)
        else:super().setCurrentText(text)

class ReviewFilters(QWidget):
    changed=Signal()
    def __init__(self,parent=None):
        super().__init__(parent);v=QVBoxLayout(self);v.setContentsMargins(0,0,0,0);v.setSpacing(3)
        self.fields={};top=QHBoxLayout();v.addLayout(top)
        self.add(top,'group','商品分组',GROUPS)
        self.more_btn=button('更多筛选',self.toggle);top.addWidget(self.more_btn)
        top.addWidget(button('重置组合筛选',self.reset));top.addStretch()
        self.more=QWidget();line=QHBoxLayout(self.more);line.setContentsMargins(0,0,0,0)
        self.add(line,'quantity','数量',['全部数量','正数量','零数量','数量待核'])
        self.add(line,'adjustment','来源',['全部来源','自动安排',*KINDS.values()])
        self.add(line,'budget','预算',['全部预算状态','无预算缺口','部分安排','预算暂缓','未启用预算'])
        self.add(line,'must','必卖',['全部优先级','仅必卖']);line.addStretch();v.addWidget(self.more);self.more.hide()
        self.condition=label('','muted');v.addWidget(self.condition);self.publish()
    def add(self,line,key,title,items):
        line.addWidget(label(title));c=CountCombo()
        for value in items:c.addItem(value,value)
        c.setToolTip(title+'；括号数字为保留其他条件时的商品款数');line.addWidget(c);self.fields[key]=c;c.currentIndexChanged.connect(self.publish)
    def set_counts(self,counts):
        for key,c in self.fields.items():
            lock=QSignalBlocker(c)
            for i in range(c.count()):c.setItemText(i,c.itemData(i)+f"（{counts[key].get('all' if i==0 else c.itemData(i),0)}）")
            del lock
    def values(self):return {key:c.currentText() for key,c in self.fields.items()}
    def publish(self,*_):
        selected=[c.currentText() for c in self.fields.values() if c.currentIndex()>0]
        self.condition.setText('组合条件：'+' ＋ '.join(selected) if selected else '组合条件：全部；不同条件同时满足。')
        self.changed.emit()
    def reset(self):
        locks=[QSignalBlocker(c) for c in self.fields.values()]
        for c in self.fields.values():c.setCurrentIndex(0)
        del locks;self.publish()
    def toggle(self):
        on=not self.more.isVisible();self.more.setVisible(on);self.more_btn.setText('收起筛选' if on else '更多筛选')
