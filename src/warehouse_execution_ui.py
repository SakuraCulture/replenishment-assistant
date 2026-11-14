"""Choose the execution scope after calculation, without changing supply routes."""
import sqlite3
from contextlib import closing
from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog,QVBoxLayout,QHBoxLayout,QTableWidget,QTableWidgetItem
from ui_controls import label,button
import warehouse_orders


def retired_warehouses(host,store):
    """Read the child fence only; never initialize settings or upgrade a DB."""
    path=getattr(getattr(host,'work_repository',None),'path',None)
    if path is None or not Path(path).is_file():return set()
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='normal_orders'").fetchone():return set()
        row=db.execute('SELECT oid FROM normal_orders WHERE day=? AND store=? AND kind=?',(host.job_day,store,'normal_model')).fetchone()
        if not row:return set()
        import warehouse_execution
        return {w for w in warehouse_orders.WAREHOUSES if (warehouse_execution.state(db,row[0],w) or {}).get('state')=='deleted'}


def initial_warehouses(host,store,available):
    selected=getattr(host,'execution_warehouses',{}).get(store)
    return list(available) if selected is None else [w for w in available if w in selected]


class ExecutionSelection(QDialog):
    def __init__(self,parent,states,initial):
        super().__init__(parent);self.setObjectName('warehouseExecutionSelection')
        self.setWindowTitle('选择本轮执行门店与仓');self.resize(1080,630)
        layout=QVBoxLayout(self)
        layout.addWidget(label('本轮执行仓：只签发勾选的仓单，另一仓不受影响。','section'))
        layout.addWidget(label('仍按门店原单双仓配置、主从顺序和商品资料计算；本轮选仓不改变商品归属、数量或预算，不把未选仓转为地采。','muted'))
        self.table=QTableWidget(0,7);self.table.setObjectName('warehouseExecutionTable')
        self.table.setHorizontalHeaderLabels(['本轮执行','门店名称','门店编码','供货仓','状态','商品款数','本仓安排金额'])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers);self.items=[]
        results=getattr(parent,'results',{});profile=getattr(parent,'new_focus',None)
        for store,state in states.items():
            summaries=[];valid=state.get('state')=='已完成' and store in results;reason=state.get('state','未完成')
            if valid:
                try:summaries=warehouse_orders.summaries(results[store],profile)
                except ValueError as exc:valid=False;reason=str(exc)
            retired=retired_warehouses(parent,store) if valid else set()
            available=[s['warehouse'] for s in summaries if s['warehouse'] not in retired]
            selected=initial_warehouses(parent,store,available)
            if not summaries:summaries=[{'warehouse':None,'moving':0,'new':0,'amount':0}];valid=False
            for summary in summaries:
                warehouse=summary['warehouse'];enabled=valid and warehouse not in retired
                n=self.table.rowCount();self.table.insertRow(n);item=QTableWidgetItem()
                item.setFlags(Qt.ItemFlag.ItemIsEnabled|Qt.ItemFlag.ItemIsUserCheckable if enabled else Qt.ItemFlag.NoItemFlags)
                item.setCheckState(Qt.CheckState.Checked if enabled and store in initial and warehouse in selected else Qt.CheckState.Unchecked)
                self.table.setItem(n,0,item);self.items.append((store,warehouse,item,enabled))
                status='已永久删除，请新建本仓任务' if warehouse in retired else '可签发' if enabled else reason
                for c,value in enumerate((state.get('name',store),store,warehouse or '—',status,summary['moving']+summary['new'],f"{summary['amount']:,.2f}"),1):
                    cell=QTableWidgetItem(str(value));cell.setToolTip(str(value));self.table.setItem(n,c,cell)
        layout.addWidget(self.table,1);self.note=label('');self.note.setObjectName('warehouseExecutionNote');layout.addWidget(self.note)
        controls=QHBoxLayout();controls.addWidget(button('取消',self.reject));self.go=button('下一步：核对所选仓单',self.accept,True);self.go.setObjectName('warehouseExecutionContinue');controls.addWidget(self.go);layout.addLayout(controls)
        self.table.itemChanged.connect(self.update_count)
        from table_readability import install,fit_window
        fit_window(self,1080,630);install(self.table);self.table.reading_controller.apply()
        for i,width in enumerate((90,260,170,110,230,100,150)):self.table.setColumnWidth(i,width)
        self.update_count()

    def warehouses(self):
        result={}
        for store,warehouse,item,enabled in self.items:
            if enabled and item.checkState()==Qt.CheckState.Checked:result.setdefault(store,[]).append(warehouse)
        return result

    def codes(self):return list(self.warehouses())

    def update_count(self,*_):
        selected=self.warehouses();count=sum(map(len,selected.values()))
        self.note.setText(f'本轮签发 {len(selected)} 家门店、{count} 张仓单；未勾选仓本轮不参与，不生成待回传事项。')
        self.go.setEnabled(count>0)
