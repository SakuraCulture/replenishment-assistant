"""Explicit source proposals, scoped to visible groups; no default winner."""
from pathlib import Path
from PySide6.QtWidgets import QHBoxLayout,QComboBox,QCheckBox,QLineEdit,QMessageBox
from ui_controls import button,label

def proposals(conflicts,choices,path,visible,replace=False):
    result=[]
    for i,c in enumerate(conflicts):
        if i not in visible or choices[i] is not None and not replace:continue
        matches=[n for n,r in enumerate(c['variants']) if r['path']==path]
        # Even a single file may contain multiple conflicting rows: never guess.
        if len(matches)==1 and matches[0]!=choices[i]:result.append((i,matches[0]))
    return result

def add_bulk_choices(dialog,layout,table,conflicts,combos):
    row=QHBoxLayout();search=QLineEdit();search.setPlaceholderText('筛选门店 / 商品 / 来源');row.addWidget(search)
    source=QComboBox()
    for path in dict.fromkeys(r['path'] for c in conflicts for r in c['variants']):source.addItem(Path(path).name,path)
    row.addWidget(source);replace=QCheckBox('包含已经选择的组');row.addWidget(replace)
    undo=[];note=label('只对当前可见冲突提出选择；同文件有多条候选时仍需逐条核对。','muted')
    def filter_rows(text):
        for i,c in enumerate(conflicts):table.setRowHidden(i,not all(t.casefold() in (c['store']+' '+c['sku']+' '+table.item(i,1).text()).casefold() for t in text.split()))
    search.textChanged.connect(filter_rows)
    def propose():
        choices=[c.currentData() for c in combos];changes=proposals(conflicts,choices,source.currentData(),{i for i in range(len(conflicts)) if not table.isRowHidden(i)},replace.isChecked())
        if not changes:note.setText('没有可批量选择的组；同一来源多条候选的组请逐条选择。');return
        message=f'拟将当前可见的 {len(changes)} 组选择为：\n{source.currentData()}\n仅更新窗口中的选择，最终仍需点击“应用选择并重新检查”。'
        box=QMessageBox(QMessageBox.Icon.Question,'核对批量来源选择',message,QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No,dialog);box.setDefaultButton(QMessageBox.StandardButton.No)
        box.setDetailedText('\n'.join(f"{conflicts[i]['store']} / {conflicts[i]['sku']}：{choices[i]} → 候选 {n+1}" for i,n in changes))
        if box.exec()!=QMessageBox.StandardButton.Yes:return
        undo.append(choices)
        for i,n in changes:combos[i].setCurrentIndex(n+1)
        note.setText(f'已提出 {len(changes)} 组选择，可撤销；未选择其他组。')
    def revert():
        if undo:
            for c,n in zip(combos,undo.pop()):c.setCurrentIndex(0 if n is None else n+1)
            note.setText('已撤销上次批量选择。')
    row.addWidget(button('按来源批量提出选择',propose));row.addWidget(button('撤销批量选择',revert));layout.addLayout(row);layout.addWidget(note)
