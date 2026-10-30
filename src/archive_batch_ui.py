"""Independent export entrypoints and explicit multi-order selection."""
import json
from pathlib import Path
from PySide6.QtCore import Qt,QSignalBlocker,QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QDialog,QVBoxLayout,QHBoxLayout,QComboBox,QCheckBox,QFileDialog,QMessageBox,QTableWidgetItem
from ui_controls import button,label
from business_ui import run,grid
from runtime_support import Cancelled
import archive_batch as batch

class Preview(QDialog):
    def __init__(self,parent,plan):
        super().__init__(parent);self.plan=plan;self.setWindowTitle('核对导出范围与修订');self.resize(1100,650)
        v=QVBoxLayout(self);v.addWidget(label(batch.TITLES[plan['kind']],'title'));self.mode=QComboBox()
        self.mode.addItems(['每个店仓单分别导出原文件','相同仓＋相同原文件只下载一次'] if plan['kind']=='original' else ['分别导出：每个店仓单一个Excel','合并导出：每仓一个Excel，同一张表包含多个订单'])
        self.mode.setCurrentIndex(0 if plan['mode']=='separate' else 1);v.addWidget(self.mode)
        rows=[]
        for item in plan['items']:
            h=item['head'];rows.append([h['day'],h.get('name',h['store']),h['warehouse'],h['number'],f'V{h["revision"]:03d}',item.get('count','—'),item['error'] or '可导出'])
        table,model=grid(self,['业务日期','门店','仓库','补货单号','修订','明细行 / 原文件数','核对状态'],rows)
        for i,width in enumerate([100,135,70,240,60,90,270]):table.setColumnWidth(i,width)
        v.addWidget(table,1)
        self.summary=label('');v.addWidget(self.summary)
        self.partial=QCheckBox('已核对缺失/失败项，仅导出可用部分（完成页保留缺口）');self.partial.setVisible(any(i['error'] for i in plan['items']));v.addWidget(self.partial)
        text='直接复制留存原Excel，保留全部工作表、样式和公式；与当前商品是否匹配无关。' if plan['kind']=='original' else '不重算、不跨单合计商品。山东河北分开；历史未留存字段留空，全店共享预算不能按行累加。'
        if plan['kind']=='reference':text+=' 这是订单相关商品的加工核对表，不是完整原Excel；公式作为原文字展示。'
        v.addWidget(label(text,'muted'));row=QHBoxLayout();row.addWidget(button('取消',self.reject));self.start=button('选择目录并开始导出',self.accept,True);row.addWidget(self.start);v.addLayout(row)
        self.mode.currentIndexChanged.connect(self.update_plan);self.partial.toggled.connect(self.update_plan);self.update_plan()
    def update_plan(self,*_):
        self.plan['mode']='combined' if self.mode.currentIndex() else 'separate';units=batch.groups(self.plan)
        valid=sum(len(u) for u in units);missing=len(self.plan['items'])-valid
        detail='；'.join(f'{u[0]["head"]["warehouse"]} {len(u)}单 → 1份' for u in units[:8])
        self.summary.setText(f'已选 {len(self.plan["items"])} 张仓单 · 可用 {valid} 单 · 不可用 {missing} 单 · 预计 {len(units)} 个文件\n'+detail+('；其余见上方全部订单' if len(units)>8 else ''))
        capacity=any(sum(i.get('count',0) for i in u)+1>batch.MAX_ROWS or len({c for i in u for c in i.get('columns',[])})+len(batch.IDENTITY)>batch.MAX_COLUMNS for u in units) if self.plan['kind']!='original' else False
        if capacity:self.summary.setText(self.summary.text()+'\n超过Excel容量，请缩小范围或分别导出；不截断。')
        self.start.setEnabled(bool(units) and (not missing or self.partial.isChecked()) and not capacity)

def show_result(parent,path,plan,result):
    d=QDialog(parent);d.setWindowTitle('导出结果 · 原归档未改变');d.resize(1000,600);v=QVBoxLayout(d)
    v.addWidget(label(f'完成 {len(result["files"])} 个文件；失败 {len(result["failed"])} 单；不可用 {len(result["skipped"])} 单；取消未执行 {len(result["cancelled"])} 单','section'))
    rows=[[Path(f['path']).name,'完成',f'{len(f["heads"])} 张仓单'] for f in result['files']]
    for state,title in [('failed','失败'),('skipped','不可用'),('cancelled','取消未执行')]:
        rows.extend([[i['head']['number']+f' V{i["head"]["revision"]:03d}',title,i.get('error') or '未执行'] for i in result[state]])
    table,model=grid(d,['文件 / 仓单','状态','说明'],rows);table.setColumnWidth(0,500);v.addWidget(table,1);v.addWidget(label(result['directory'],'muted'))
    row=QHBoxLayout();row.addWidget(button('打开导出目录',lambda:QDesktopServices.openUrl(QUrl.fromLocalFile(result['directory']))))
    retry=result['failed']+result['cancelled']
    def again():
        d.accept();export(parent,path,[i['head'] for i in retry],plan['kind'],plan['mode'])
    if retry:row.addWidget(button('仅重试失败 / 未执行项（原修订）',again))
    row.addWidget(button('关闭',d.accept));v.addLayout(row);d.exec()

def export(parent,path,heads,kind,mode=None):
    if not heads:QMessageBox.information(parent,'请勾选仓单','请勾选要导出的仓单，可跨页选择或勾选全部查询结果。');return
    if any(h.get('deleted') for h in heads):QMessageBox.information(parent,'请先恢复','回收站单据恢复后才能导出。');return
    prefs=Path(path).parent/'archive_export_ui.json'
    if mode is None:
        try:mode=json.loads(prefs.read_text(encoding='utf-8')).get(kind,'separate')
        except (OSError,ValueError):mode='separate'
    try:
        plan=run(parent,batch.prepare,(path,heads,kind,mode),'核对历史导出范围')
        preview=Preview(parent,plan)
        if preview.exec()!=QDialog.DialogCode.Accepted:return
        folder=QFileDialog.getExistingDirectory(parent,'选择导出目录（自动新建本次任务文件夹）')
        if not folder:return
        try:
            from priority_list import atomic_json
            data=json.loads(prefs.read_text(encoding='utf-8')) if prefs.exists() else {};data[kind]=plan['mode'];atomic_json(prefs,data)
        except (OSError,ValueError):pass # UI preference failure cannot affect historical data/export.
        result=run(parent,batch.execute,(path,plan,folder,preview.partial.isChecked()),'导出'+batch.TITLES[kind])
        show_result(parent,path,plan,result)
    except Cancelled:return
    except Exception as exc:QMessageBox.warning(parent,'导出未完成',str(exc))

def original(parent,path,heads):
    d=QDialog(parent);d.setWindowTitle('商品资料：请选择实际产物');v=QVBoxLayout(d)
    v.addWidget(label('原文件副本保留原Excel；核对表是加工表，仅包含所选订单相关商品。'))
    choice=QComboBox();choice.addItems(['原文件副本（默认，原样下载）','多订单资料核对表（加工表，不是原文件）']);v.addWidget(choice)
    row=QHBoxLayout();row.addWidget(button('取消',d.reject));row.addWidget(button('下一步：核对文件',d.accept));v.addLayout(row)
    if d.exec()==QDialog.DialogCode.Accepted:export(parent,path,heads,'reference' if choice.currentIndex() else 'original')

class ArchiveBatchActions:
    def setup_archive_exports(self,layout):
        self.archive_checked={};row=QHBoxLayout();self.archive_export_buttons=[]
        for title,kind in [('导出完整计算表','calculation'),('下载商品资料原表','original'),('导出签发建议','signed')]:
            b=button(title,lambda checked=False,k=kind:self.export_archive_batch(k));row.addWidget(b);self.archive_export_buttons.append(b)
        layout.addLayout(row);self.archive_selection_note=label('已选 0 张仓单','muted');layout.addWidget(self.archive_selection_note)
        row=QHBoxLayout()
        for title,action in [('勾选本页',lambda:self.check_archive(self.archive_orders)),('勾选全部查询结果',lambda:self.check_archive([h for h,m in zip(self.archive_all,self.archive_matches) if m])),('取消勾选',self.clear_archive_selection),('已选明细',self.show_archive_selection)]:row.addWidget(button(title,action))
        layout.addLayout(row)
    def decorate_archive_selection(self):
        table=self.archive_table;block=QSignalBlocker(table);table.insertColumn(0);table.setHorizontalHeaderItem(0,QTableWidgetItem('勾选'));table.insertColumn(table.columnCount());table.setHorizontalHeaderItem(table.columnCount()-1,QTableWidgetItem('原文件状态'))
        for i,h in enumerate(self.archive_orders):
            cell=QTableWidgetItem();cell.setFlags(Qt.ItemFlag.ItemIsEnabled|Qt.ItemFlag.ItemIsUserCheckable);cell.setCheckState(Qt.CheckState.Checked if batch.key(h) in self.archive_checked else Qt.CheckState.Unchecked);table.setItem(i,0,cell)
            table.setItem(i,table.columnCount()-1,QTableWidgetItem(h.get('source_status','导出前核验')))
        del block;table.resizeColumnsToContents();self.selection_status()
    def archive_check_changed(self,item):
        if item.column()!=0 or item.row()>=len(self.archive_orders):return
        h=self.archive_orders[item.row()];k=batch.key(h)
        if item.checkState()==Qt.CheckState.Checked:self.archive_checked[k]=dict(h)
        else:self.archive_checked.pop(k,None)
        self.selection_status()
    def selection_status(self):
        hs=list(self.archive_checked.values());self.archive_selection_note.setText(f'已选 {len(hs)} 张仓单 · {len({h["store"] for h in hs})} 家店 · {len({h["day"] for h in hs})} 个业务日期；导出前核对确切修订与文件数')
        for b in self.archive_export_buttons:b.setEnabled(bool(hs) and not self.archive_trash.currentIndex() and not getattr(self,'archive_query_pending',False))
    def check_archive(self,heads):
        if getattr(self,'archive_query_pending',False):return
        for h in heads:self.archive_checked[batch.key(h)]=dict(h)
        self.render_archive_page()
    def clear_archive_selection(self):
        self.archive_checked.clear();self.render_archive_page()
    def show_archive_selection(self):
        d=QDialog(self);d.setWindowTitle('已选仓单（导出顺序）');d.resize(1000,550);v=QVBoxLayout(d)
        table,model=grid(d,['日期','门店','仓库','单号','修订'],[[h['day'],h['store'],h['warehouse'],h['number'],h['revision']] for h in self.archive_checked.values()]);v.addWidget(table);v.addWidget(button('关闭',d.accept));d.exec()
    def export_archive_batch(self,kind,heads=None):
        heads=list(self.archive_checked.values()) if heads is None else heads
        if kind=='original':original(self,self.work_repository.path,heads)
        else:export(self,self.work_repository.path,heads,kind)
