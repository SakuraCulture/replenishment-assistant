"""Explicit filter editor, leaving the business table as the main work area."""
import copy
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog,QVBoxLayout,QHBoxLayout,QGridLayout,QListWidget,QListWidgetItem,QLineEdit,QComboBox,QLabel
from ui_controls import button
import final_list_filters as rules


class FilterDialog(QDialog):
    def __init__(self, rows, spec, parent=None):
        super().__init__(parent); self.setWindowTitle('类目 / 销量 / 库存筛选'); self.resize(780,580)
        self.spec=copy.deepcopy(spec); v=QVBoxLayout(self)
        info=QLabel('同类目多选为“或”；类目、模型分类及各数值条件同时满足。未勾选类目表示不限。数量均按库存单位。')
        info.setWordWrap(True);v.addWidget(info);line=QHBoxLayout();self.lists={}
        for key,title,model in [('categories','商品原始类目',False),('profiles','补货模型分类',True)]:
            col=QVBoxLayout();col.addWidget(QLabel(title));search=QLineEdit();search.setPlaceholderText('搜索分类（不改变已勾选项）');col.addWidget(search)
            listing=QListWidget();col.addWidget(listing);self.lists[key]=listing
            values={rules.category(d,model) for d in rows};values.update(spec.get(key,[]))
            for value in sorted(values,key=lambda x:(x is not None,str(x))):
                item=QListWidgetItem('资料缺失' if value is None else value);item.setData(Qt.ItemDataRole.UserRole,value)
                item.setFlags(item.flags()|Qt.ItemFlag.ItemIsUserCheckable);item.setCheckState(Qt.CheckState.Checked if value in spec.get(key,[]) else Qt.CheckState.Unchecked);listing.addItem(item)
            search.textChanged.connect(lambda text,w=listing:[w.item(i).setHidden(text.casefold() not in w.item(i).text().casefold()) for i in range(w.count())])
            col.addWidget(button('清空本组勾选（不限）',lambda _,w=listing:[w.item(i).setCheckState(Qt.CheckState.Unchecked) for i in range(w.count())]));line.addLayout(col)
        v.addLayout(line,1);grid=QGridLayout();self.numbers={}
        for i,(key,title) in enumerate(rules.FIELDS.items()):
            op=QComboBox()
            for code,name in rules.OPS.items():op.addItem(name,code)
            low=QLineEdit();low.setPlaceholderText('数值 / 区间下限');high=QLineEdit();high.setPlaceholderText('区间上限')
            r=spec.get('numbers',{}).get(key,{});op.setCurrentIndex(max(0,op.findData(r.get('op','any'))));low.setText(str(r.get('low','')));high.setText(str(r.get('high','')))
            def enable(_,o=op,l=low,h=high):l.setEnabled(o.currentData() not in ('any','missing'));h.setEnabled(o.currentData()=='between')
            op.currentIndexChanged.connect(enable);enable(0)
            grid.addWidget(QLabel(title),i,0);grid.addWidget(op,i,1);grid.addWidget(low,i,2);grid.addWidget(high,i,3);self.numbers[key]=(op,low,high)
        v.addLayout(grid);self.error=QLabel();self.error.setWordWrap(True);v.addWidget(self.error)
        actions=QHBoxLayout();actions.addWidget(button('重置本组条件',self.reset));actions.addStretch();actions.addWidget(button('取消',self.reject));actions.addWidget(button('应用筛选',self.accept,True));v.addLayout(actions)
        from table_readability import fit_window
        fit_window(self,780,580)
    def reset(self):
        for w in self.lists.values():
            for i in range(w.count()):w.item(i).setCheckState(Qt.CheckState.Unchecked)
        for op,low,high in self.numbers.values():op.setCurrentIndex(0);low.clear();high.clear()
    def accept(self):
        spec={key:[w.item(i).data(Qt.ItemDataRole.UserRole) for i in range(w.count()) if w.item(i).checkState()==Qt.CheckState.Checked] for key,w in self.lists.items()}
        spec['numbers']={key:{'op':op.currentData(),'low':low.text().strip(),'high':high.text().strip()} for key,(op,low,high) in self.numbers.items() if op.currentData()!='any'}
        try:rules.validate(spec)
        except ValueError as e:self.error.setText(str(e));return
        self.spec=spec;super().accept()
