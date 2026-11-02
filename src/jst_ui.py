"""Daily execution UI. Frozen previews, fixed actions, responsive bounded workers."""
from pathlib import Path
from decimal import Decimal
from contextlib import closing
import sqlite3
from PySide6.QtCore import Qt,QTimer
from PySide6.QtWidgets import (QDialog,QVBoxLayout,QHBoxLayout,QComboBox,QListWidget,QListWidgetItem,
    QAbstractItemView,QCheckBox,QProgressBar,QMessageBox,QInputDialog,QLineEdit,QTabWidget)
from ui_controls import button,label
from business_ui import run,grid
from ui_tasks import Worker
from interaction_ui import confirm
from lifecycle_ui import warn,Review,commit as commit_lifecycle
import jst_orders as service
import jst_exports
import daily_batch
import export_locations
import warehouse_catalog
from jst_review_ui import AllocationReview


def selected_ids(path, heads, progress=lambda _:None,cancel=None):
    from runtime_support import checkpoint
    result=[]
    with closing(sqlite3.connect(path)) as db:
        for h in heads:
            checkpoint(cancel);gid=service.active(db,h)
            if gid:result.append(gid)
    return result


def export_orders(parent,host,heads):
    try:
        gids=run(parent,selected_ids,(host.work_repository.path,heads),'读取所选生成单')
        if not gids:QMessageBox.information(parent,'尚未生成','所选订单没有可导出的聚水潭生成记录。');return
        folder=export_locations.initial_directory(parent)
        if not confirm(parent,'导出聚水潭导入单',f'所选{len(heads)}单，可导出{len(gids)}单；其余未生成将跳过。商品编码、单价、数量、订单号四列，每店每仓一份，同名安全覆盖。\n'+folder):return
        r=run(parent,jst_exports.export,(host.work_repository.path,gids,folder),'导出聚水潭订单')
        QMessageBox.information(parent,'导出结果',f"成功{len(r['files'])}份，失败{len(r['failed'])}份，取消：{r['cancelled']}\n"+'\n'.join([*(x['error'] for x in r['failed']),*r.get('warnings',[])]))
    except Exception as exc:warn(parent,exc)


def payment(parent,host,head):
    try:
        gids=run(parent,selected_ids,(host.work_repository.path,[head]),'读取付款草稿')
        if not gids:raise ValueError('未找到生成单，请先生成或导入真实付款单')
        plan=run(parent,service.payment_plan,(host.work_repository.path,gids[0]),'核对付款草稿')
        d=Review(parent,plan);d.setWindowTitle('导入门店付款单 · 使用聚水潭导入单')
        d.layout().insertWidget(0,label('只有数量和金额与实际已付款订单一致，才确认保存。存在差异请取消，使用“导入门店付款单”。','section'))
        if d.exec():commit_lifecycle(parent,host,plan)
        d.deleteLater()
    except Exception as exc:warn(parent,exc)


class GenerationDialog(QDialog):
    def __init__(self,parent,host,heads):
        super().__init__(parent);self.host=host;self.path=host.work_repository.path;self.heads=list(heads);self.plan=None;self.worker=None;self.export_worker=None;self.export_queue=[];self.saved=[];self.close_pending=False;self.committing=False;self.catalogs={};self.receipt=None;self.setObjectName('jstGeneration')
        self._completed=False;self.stop_requested=False
        self.setWindowTitle('生成聚水潭订单 · 按回传分配');self.resize(1220,820);self.setAcceptDrops(True)
        v=QVBoxLayout(self);v.addWidget(label('选单和仓资料 → 核对分配 → 保存并导出；已锁单跳过，库存不足一箱分配0。','section'))
        top=QHBoxLayout();self.warehouse=QComboBox();self.warehouse.addItems(warehouse_catalog.WAREHOUSES);top.addWidget(self.warehouse)
        self.update_btn=button('更新本次仓资料',self.choose);top.addWidget(self.update_btn);self.default_btn=button('采用已保存仓资料',self.defaults);top.addWidget(self.default_btn);top.addWidget(button('下载仓资料模板',self.template));self.save_default_btn=button('保存为该仓默认资料',self.save_default);top.addWidget(self.save_default_btn);v.addLayout(top)
        self.source=label('正在读取仓资料…','muted');self.source.setWordWrap(True);v.addWidget(self.source)
        self.order_list=QListWidget();self.order_list.setMaximumHeight(90);self.order_list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        for h in heads:
            item=QListWidgetItem(h['number']+' · '+h.get('name',h['store'])+' · '+h['warehouse']);item.setData(Qt.ItemDataRole.UserRole,h);self.order_list.addItem(item)
        v.addWidget(self.order_list);self.order_list.model().rowsMoved.connect(self.invalidate)
        options=QHBoxLayout();self.manual=QCheckBox('使用上方人工顺序（可拖动）；未勾选按回传确认时间优先');self.correction=QCheckBox('重新分配所选未锁单（须选齐该仓全部未锁生成单）');options.addWidget(self.manual);options.addWidget(self.correction);v.addLayout(options);self.manual.toggled.connect(self.invalidate);self.correction.toggled.connect(self.invalidate)
        self.preview_btn=button('预览库存分配',self.preview,True);v.addWidget(self.preview_btn)
        self.summary=label('预览不会占用库存。新资料已扣此前已锁单；未锁预留继续保留。','muted');self.summary.setWordWrap(True);v.addWidget(self.summary)
        self.review=AllocationReview(self);v.addWidget(self.review,1)
        self.accept_issues=QCheckBox('已核对缺口及待核原因，同意待核商品本次不安排');v.addWidget(self.accept_issues)
        self.auto_export=QCheckBox('每单保存后立即导出，后续继续生成');self.auto_export.setChecked(False);v.addWidget(self.auto_export)
        self.bar=QProgressBar();self.bar.setRange(0,1);v.addWidget(self.bar);self.status=label('待预览','muted');self.status.setWordWrap(True);v.addWidget(self.status)
        actions=QHBoxLayout();self.generate_btn=button('确认生成',self.generate,True);self.generate_btn.setEnabled(False);actions.addWidget(self.generate_btn);self.stop_btn=button('停止剩余任务',self.stop);self.stop_btn.setEnabled(False);actions.addWidget(self.stop_btn);actions.addWidget(button('关闭',self.reject));v.addLayout(actions)
        self.completed=QListWidget();self.completed.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection);self.completed.setMaximumHeight(115);v.addWidget(self.completed)
        self.completed.hide()
        actions=QHBoxLayout();actions.addWidget(button('导出所选已生成单',self.export_saved));actions.addWidget(button('确认所选已下单并锁定',self.lock_saved));v.addLayout(actions)
        self.warehouse.currentIndexChanged.connect(self.describe);QTimer.singleShot(0,self.load_catalogs)
    def load_catalogs(self):
        try:self.catalogs=run(self,service.catalogs,(self.path,self.host.repository.folder),'读取仓资料');self.describe()
        except Exception as exc:warn(self,exc)
    def describe(self):
        c=self.catalogs.get(self.warehouse.currentText())
        if not c:self.source.setText('该仓未提供资料；请更新或维护默认仓资料。');return
        x=c['catalog'];self.source.setText(f"{x['warehouse']} · {x.get('source_path','')} · 导入 {x.get('imported_at','')} · {len(x['items'])}款 · "+('当前库存基准，可直接预览' if c['bid'] else '默认资料尚未建立库存基准，请点击采用'))
    def invalidate(self,*_):
        self.plan=None;self.generate_btn.setEnabled(False);self.summary.setText('资料或顺序变化，请重新预览。')
        self.review.replace([],'原预览已失效，请重新预览')
    def choose(self):
        from PySide6.QtWidgets import QFileDialog
        p,_=QFileDialog.getOpenFileName(self,'更新'+self.warehouse.currentText()+'资料','','Excel (*.xlsx)')
        if p:self.refresh_stock(p)
    def refresh_stock(self,path=None,catalog=None):
        if self.worker: return
        try:
            p=run(self,service.prepare_refresh,(self.path,self.warehouse.currentText(),path,catalog),'读取仓资料并校验')
            run(self,service.commit_refresh,(self.path,p),'更新库存基准')
            self.invalidate();self.load_catalogs()
        except Exception as exc:warn(self,exc)
    def defaults(self):
        c=self.catalogs.get(self.warehouse.currentText())
        if not c:QMessageBox.information(self,'缺少默认资料','请先导入该仓商品资料。');return
        if c['bid']:self.describe();return
        self.refresh_stock(catalog=c['catalog'])
    def save_default(self):
        c=self.catalogs.get(self.warehouse.currentText())
        if c and confirm(self,'保存为默认仓资料','保存'+self.warehouse.currentText()+'资料供下次使用；历史单据不变。'):
            try:warehouse_catalog.Repository(self.host.repository.folder).save(c['catalog'])
            except Exception as exc:warn(self,exc)
    def template(self):
        import business_excel
        p,_=export_locations.save_file(self,'下载仓资料最低模板','仓商品资料模板.xlsx','Excel (*.xlsx)')
        if p:
            try:business_excel.write(p,[('商品资料',warehouse_catalog.FIELDS,[])])
            except Exception as exc:warn(self,exc)
    def dragEnterEvent(self,event):
        u=event.mimeData().urls()
        if not self.worker and len(u)==1 and u[0].isLocalFile() and Path(u[0].toLocalFile()).suffix.lower()=='.xlsx':event.acceptProposedAction()
    def dropEvent(self,event):self.refresh_stock(event.mimeData().urls()[0].toLocalFile());event.acceptProposedAction()
    def preview(self):
        try:
            heads=[self.order_list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.order_list.count())]
            # Adopt default only once per warehouse; no reset on subsequent previews.
            for wh in dict.fromkeys(h['warehouse'] for h in heads):
                c=self.catalogs.get(wh)
                if c and not c['bid']:
                    p=run(self,service.prepare_refresh,(self.path,wh,None,c['catalog']),'采用默认仓资料')
                    run(self,service.commit_refresh,(self.path,p),'建立库存基准');c['bid']=p['payload']['bid']
            self.describe()
            self.plan=run(self,service.prepare,(self.path,heads,self.manual.isChecked(),self.correction.isChecked()),'预览库存分配')
            v=self.plan['payload'];self.review.replace(v['plans'])
            self.summary.setText(f"本次预览 {len(v['plans'])} 张仓单；跳过 {len(v['skipped'])} 张（不计入本次汇总）。"+'；'.join(s['head']['number']+'：'+s['reason'] for s in v['skipped']))
            self.generate_btn.setEnabled(bool(v['plans']));self.accept_issues.setChecked(False)
        except Exception as exc:self.plan=None;self.review.replace([],'预览失败，请重新预览');self.generate_btn.setEnabled(False);warn(self,exc)
    def generate(self):
        if not self.plan or self.worker:return
        v=self.plan['payload']
        if any(p['summary']['issues'] for p in v['plans']) and not self.accept_issues.isChecked():QMessageBox.warning(self,'请核对待核商品','请先核对并勾选待核商品本次不安排。');return
        if not confirm(self,'确认生成聚水潭订单',f"将按预览顺序生成{len(v['plans'])}单，保存库存预留与待核对付款草稿；并不表示已经付款。\n"+'\n'.join(p['head']['number']+'：'+p['summary']['amount']+'元' for p in v['plans'])):return
        self.folder=export_locations.initial_directory(self);self.saved=[];self.completed.clear();self.completed.show();self.receipt=None;self.stop_requested=False
        w=Worker(service.commit,(self.path,self.plan,self.accept_issues.isChecked()),self);self.worker=w
        w.status.connect(self.status.setText);w.row_progress.connect(self.progress);w.commit_state.connect(self.boundary);w.business_event.connect(self.saved_order);w.success.connect(self.generation_done);w.failed.connect(lambda s:warn(self,RuntimeError(s)));w.cancelled.connect(lambda:self.status.setText('已停止，已保存订单保留'));w.finished.connect(self.worker_finished)
        for b in (self.generate_btn,self.preview_btn,self.update_btn,self.default_btn,self.save_default_btn,self.manual,self.correction,self.order_list,self.auto_export):b.setEnabled(False)
        self.stop_btn.setEnabled(True);w.start()
    def progress(self,n,total):self.bar.setRange(0,max(1,total) if total else 0);self.bar.setValue(n)
    def boundary(self,on):
        self.committing=on;self.stop_btn.setEnabled(not on);self.stop_btn.setText('正在提交，暂不能停止' if on else '停止剩余任务')
        if not on and self.close_pending:self.stop()
    def saved_order(self,value):
        if not value['positive']:return
        gid=value['gid']
        if gid in self.saved:return
        self.saved.append(gid);item=QListWidgetItem(value['number']+' · 已保存可导出');item.setData(Qt.ItemDataRole.UserRole,gid);self.completed.addItem(item)
        if self.auto_export.isChecked() and not self.stop_requested:self.export_queue.append(gid);self.dispatch_export()
    def dispatch_export(self):
        if self.export_worker or not self.export_queue:return
        gid=self.export_queue.pop(0);w=Worker(jst_exports.export,(self.path,[gid],self.folder),self);self.export_worker=w
        w.success.connect(self.export_done);w.failed.connect(lambda s:warn(self,RuntimeError(s)));w.finished.connect(self.export_finished);w.start()
    def export_done(self,r):
        for f in r['files']:
            for i in range(self.completed.count()):
                x=self.completed.item(i)
                if x.data(Qt.ItemDataRole.UserRole)==f['gid']:x.setText(x.text().split(' · ')[0]+' · 已导出')
        if r['failed']:self.status.setText('订单已保存；导出失败，可选中原单重试：'+r['failed'][0]['error'])
        if r.get('warnings'):self.status.setText('；'.join(r['warnings']))
    def export_finished(self):
        w=self.export_worker;self.export_worker=None;w.deleteLater();self.dispatch_export()
        if self.close_pending and not self.worker and not self.export_worker:self._completed=True;self.accept()
    def generation_done(self,r):
        plans=self.plan['payload']['plans'] if self.plan else []
        committed=[p for p in plans if p['gid'] in r['generated'] or p['head']['number'] in r['zero']]
        self.review.replace(committed,'本次已保存分配结果（含零分配）')
        self.summary.setText(f"已保存 {len(committed)} / {len(plans)} 张仓单；取消、失败或尚未处理的预览不计入上方结果。")
        self.receipt=r;self.status.setText(f"已保存{len(r['generated'])}单，零分配{len(r['zero'])}单，失败/未处理{len(r['errors'])}项，停止：{r['cancelled']}。"+'；'.join(x['error'] for x in r['errors'])+r.get('audit_warning',''))
    def worker_finished(self):
        w=self.worker;self.worker=None;w.deleteLater();self.committing=False;self.stop_btn.setEnabled(False)
        for b in (self.preview_btn,self.update_btn,self.default_btn,self.save_default_btn,self.manual,self.correction,self.order_list,self.auto_export):b.setEnabled(True)
        self.plan=None;self.generate_btn.setEnabled(False)
        if self.close_pending and not self.export_worker:self._completed=True;self.accept()
    def stop(self):
        if self.committing:return
        self.stop_requested=True
        if self.worker:self.worker.cancel.set()
        if self.export_worker:self.export_worker.cancel.set()
        self.export_queue.clear();self.status.setText('已请求停止；保留已提交单和已导出文件。');self.stop_btn.setEnabled(False)
    def gids(self):return [i.data(Qt.ItemDataRole.UserRole) for i in self.completed.selectedItems()]
    def export_saved(self):
        ids=self.gids()
        if not ids:QMessageBox.information(self,'请选择已生成单','在下方完成列表选择要导出的订单，可多选。');return
        self.folder=export_locations.initial_directory(self);self.export_queue.extend(g for g in ids if g not in self.export_queue);self.dispatch_export()
    def lock_saved(self):
        ids=self.gids()
        if not ids:return
        if not confirm(self,'确认已在聚水潭下单',f'锁定所选{len(ids)}单，后续不再分配；不代表已付款。'):return
        try:run(self,service.lock,(self.path,ids),'确认下单并锁定');self.status.setText('所选单已锁定，后续跳过；当前库存余额不再扣减或加回。')
        except Exception as exc:warn(self,exc)
    def reject(self):
        if self.worker or self.export_worker:self.close_pending=True;self.stop();return
        self._completed=True
        super().reject()
    def closeEvent(self,event):
        if self.worker or self.export_worker:self.reject();event.ignore()
        else:self._completed=True;event.accept()


def generate(parent,host,heads):
    if not heads:QMessageBox.information(parent,'请选择订单','先勾选要生成的店仓订单。');return
    d=GenerationDialog(parent,host,heads);previous=getattr(host,'_large_io_dialog',None);host._large_io_dialog=d
    try:d.exec()
    finally:host._large_io_dialog=previous;d.deleteLater()
    if host.closing:QTimer.singleShot(0,host.close)


def change_lock(parent,host,heads,unlock=False):
    try:
        gids=run(parent,selected_ids,(host.work_repository.path,heads),'读取所选生成单')
        if not gids:raise ValueError('所选单尚未生成')
        reason=''
        if unlock:
            reason,ok=QInputDialog.getText(parent,'更正已锁订单','说明外部撤销／更正情况及原因；本地操作不撤销远端订单：')
            if not ok:return
        if not confirm(parent,'更正已锁订单' if unlock else '确认已在聚水潭下单',f'所选可处理{len(gids)}单；锁定不确认付款，解除保护不自动加回库存。'):return
        run(parent,service.lock,(host.work_repository.path,gids,reason,unlock),'保存锁单状态')
    except Exception as exc:warn(parent,exc)


def cancel_orders(parent,host,heads):
    try:
        gids=run(parent,selected_ids,(host.work_repository.path,heads),'读取所选生成单')
        if not gids:raise ValueError('所选单尚未生成')
        reason,ok=QInputDialog.getText(parent,'撤销未下单生成单','填写原因；仅用于未下单订单，已锁单须先核对外部更正：')
        if not ok:return
        if not confirm(parent,'撤销未下单生成单',f'撤销所选{len(gids)}份生成与付款草稿的当前有效引用，历史保留；释放尚未包含在仓资料中的预留，原回传不变。'):return
        run(parent,service.cancel_generated,(host.work_repository.path,gids,reason),'撤销未下单生成单')
    except Exception as exc:warn(parent,exc)


def detail(parent,host,heads):
    try:
        gids=run(parent,selected_ids,(host.work_repository.path,heads),'读取聚水潭订单')
        if not gids:raise ValueError('所选单尚未生成')
        d=QDialog(parent);d.setWindowTitle('聚水潭分配依据 · 门店汇总');d.resize(1250,820);v=QVBoxLayout(d)
        note=label('','muted');note.setWordWrap(True)
        review=AllocationReview(d);v.addWidget(review,1)
        v.addWidget(label('当时资料原件：选择对应仓单后下载，不使用当前资料替代。','muted'));choice=QComboBox();v.addWidget(choice);v.addWidget(note)
        values=[]
        def load(i):
            p=values[i];note.setText(p['head']['number']+' · '+p['head']['warehouse']+' · 生成 '+p['saved']+' · 资料 '+p['catalog'].get('source_path',''))
        for gid in gids:
            p=run(parent,service.get,(host.work_repository.path,gid),'读取当时分配依据');values.append(p);choice.addItem(p['head']['number'])
        choice.currentIndexChanged.connect(load);load(0);review.replace(values,'所选已生成仓单（历史分配结果）')
        def source():
            target,_=export_locations.save_file(d,'下载当时仓商品资料原表','当时仓商品资料.xlsx','Excel (*.xlsx)')
            if target:
                try:run(d,service.export_source,(host.work_repository.path,gids[choice.currentIndex()],target),'读取当时原件')
                except Exception as exc:warn(d,exc)
        v.addWidget(button('下载当时仓商品资料原表',source));v.addWidget(button('关闭',d.accept));d.exec();d.deleteLater()
    except Exception as exc:warn(parent,exc)


class Logs(QDialog):
    def __init__(self,parent,host,start,end):
        super().__init__(parent);self.host=host;self.start=start;self.end=end;self.page=0;self.setWindowTitle('日批次作业日志');self.resize(1120,720);v=QVBoxLayout(self)
        self.search=QLineEdit();self.search.setPlaceholderText('门店 / 补货单号 / 仓 / 动作 / 结果');v.addWidget(self.search)
        row=QHBoxLayout();row.addWidget(button('查询',self.query));row.addWidget(button('上一页',lambda:self.turn(-1)));row.addWidget(button('下一页',lambda:self.turn(1)));row.addWidget(button('导出当前查询日志',self.export));v.addLayout(row)
        self.table,self.model=grid(self,['业务日期','操作时间','门店','仓','单号','环节','动作']);v.addWidget(self.table,1)
        from PySide6.QtWidgets import QCheckBox
        from business_presenter import HEADERS
        self.technical=QCheckBox('展开完整技术字段');v.addWidget(self.technical);self.technical.toggled.connect(lambda *_:self.show_detail(self.table.currentIndex()))
        self.detail,self.detail_model=grid(self,HEADERS);v.addWidget(self.detail,1);self.table.clicked.connect(self.show_detail);self.search.returnPressed.connect(self.query);QTimer.singleShot(0,self.query)
        self.search.setPlaceholderText('门店编码 / 补货单号 / 仓 / 动作 / 结果（与日志导出同一查询范围）')
    def turn(self,delta):self.page=max(0,self.page+delta);self.query(keep_page=True)
    def query(self,*_,keep_page=False):
        try:
            if not keep_page:self.page=0
            from business_log_views import log_page
            result=run(self,log_page,(self.host.work_repository.path,self.start,self.end,self.search.text(),self.page),'查询批次日志');self.records=result['rows'];self.page=result['page']
            self.model.replace([[r.get('display_name',r['store']) if k=='store' else r[k] for k in ('day','at','store','warehouse','number','group','action')] for r in self.records]);self.detail_model.replace([])
            self.setWindowTitle(f"日批次作业日志 · 共{result['count']}条 · 第{self.page+1}页，每页50条；导出包含全部查询结果")
        except Exception as exc:warn(self,exc)
    def show_detail(self,index):
        if not index.isValid():return
        from business_log_views import detail_rows
        r=self.records[self.model.indices[index.row()]];self.detail_model.replace(detail_rows(r['detail'],self.technical.isChecked()))
    def export(self):
        p,_=export_locations.save_file(self,'导出批次作业日志','批次作业日志.xlsx','Excel (*.xlsx)')
        if p:
            try:run(self,daily_batch.export_logs,(self.host.work_repository.path,p,self.start,self.end,self.search.text()),'导出日志')
            except Exception as exc:warn(self,exc)
