"""Two focused summary export actions; exact selected or full queried scope."""
import copy
from datetime import datetime
from PySide6.QtWidgets import QDialog,QVBoxLayout,QHBoxLayout,QComboBox,QMessageBox
from ui_controls import label,button
from business_ui import run
import export_locations,lifecycle_comparison as service

class Scope(QDialog):
    def __init__(self,parent,level,selected,count):
        super().__init__(parent);self.setObjectName('lifecycleComparisonScope');self.setWindowTitle('导出'+('订单级对比' if level=='order' else '门店级对比'));self.resize(720,280)
        v=QVBoxLayout(self);v.addWidget(label('货值统一按签发价比较；实际付款金额单列。未导入与已确认0分开。','section'))
        self.scope=QComboBox()
        if selected:self.scope.addItem(f'仅勾选的 {selected} 张订单（含跨页勾选）','selected')
        if count:self.scope.addItem(f'当前查询筛选的全部 {count} 张订单（跨所有页）','filtered')
        v.addWidget(self.scope);v.addWidget(label('门店汇总仅包含上述范围内的订单，同时附门店分仓、订单对比和差异商品。' if level=='store' else '每张店仓单一行，附差异商品和实际付款金额；不拆成多个文件。','muted'))
        row=QHBoxLayout();back=button('取消',self.reject);back.setDefault(True);row.addWidget(back);row.addWidget(button('选择保存位置并导出',self.accept,True));v.addLayout(row)

def open_export(widget,level):
    host=widget.host
    if host.closing:return
    selected=sorted(widget.checked.values(),key=lambda h:h.get('list_position',0));request=copy.deepcopy(getattr(widget,'applied_request',None));count=getattr(widget,'count',0)
    if not selected and (not count or request is None):QMessageBox.information(widget,'暂无可导出的订单','先查询订单，再选择需要的范围。');return
    dialog=Scope(widget,level,len(selected),count if request else 0)
    if dialog.exec()!=QDialog.DialogCode.Accepted:dialog.deleteLater();return
    scope=dialog.scope.currentData();dialog.deleteLater()
    title='订单级生命周期对比' if level=='order' else '门店级生命周期对比'
    target,_=export_locations.save_file(widget,'保存'+title,title+'_'+datetime.now().strftime('%Y%m%d_%H%M%S')+'.xlsx','Excel (*.xlsx)')
    if not target:return
    try:
        if scope=='filtered':
            heads=run(widget,service.all_heads,(host.work_repository.path,host.repository.folder/'lifecycle_index.sqlite3',request),'读取全部筛选订单')
            # Scope changed after the list query: do not silently export another count.
            if len(heads)!=count:raise ValueError('当前筛选订单数量已变化，请刷新后重新导出')
            fields={'from':'开始日期','to':'结束日期','search':'搜索内容','warehouse':'仓库','status':'订单进度','stage_view':'工作环节','stage_filter':'环节筛选','lock':'锁单状态'}
            stages={'return':'回传处理','payment':'付款核对','receipt':'收货核对','jst':'聚水潭导入单'}
            scope_text='当前筛选全部；'+ '；'.join(f'{fields[k]}：{stages.get(v,v) if k=="stage_view" else v}' for k,v in request.items() if k in fields and v)
        else:heads=selected;scope_text=f'勾选的{len(heads)}张订单（含跨页勾选）'
        receipt=run(widget,service.export,(host.work_repository.path,heads,target,level,scope_text),'导出'+title)
        from readable_ui import file_results
        file_results(widget,[receipt['path']],'对比报表已导出',f"{receipt['stores']}家门店、{receipt['orders']}张店仓单；货值按签发价。")
        return receipt
    except Exception as exc:
        from runtime_support import Cancelled
        if isinstance(exc,Cancelled):host.status.setText('对比导出已取消，原订单未变。')
        else:
            from lifecycle_ui import warn
            warn(widget,exc)
