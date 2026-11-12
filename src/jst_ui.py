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
import jst_batches as batches
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
        r=run(parent,jst_exports.export_checked,(host.work_repository.path,gids,folder),'导出聚水潭订单')
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
    def __init__(self,parent,host,heads,batch_id=None,correction=False):
        super().__init__(parent);self.batch_id=batch_id;self.correction_mode=correction;self.host=host;self.path=host.work_repository.path;self.heads=list(heads);self.plan=None;self.worker=None;self.export_worker=None;self.export_queue=[];self.saved=[];self.close_pending=False;self.committing=False;self.catalogs={};self.receipt=None;self.setObjectName('jstGeneration')
        self._completed=False;self.stop_requested=False
        self.setWindowTitle('重新分配本批所选' if correction else '继续原分配批次' if batch_id else '准备新分配批次');self.resize(1220,820);self.setAcceptDrops(True)
        v=QVBoxLayout(self);v.addWidget(label('选订单和本批仓资料 → 预览 → 分配 → 导出；每批库存独立，同批按顺序扣减。','section'))
        top=QHBoxLayout();self.warehouse=QComboBox();self.warehouse.addItems(warehouse_catalog.WAREHOUSES);top.addWidget(self.warehouse)
        self.update_btn=button('更新本次仓资料',self.choose);top.addWidget(self.update_btn);self.default_btn=button('采用已保存仓资料',self.defaults);top.addWidget(self.default_btn);top.addWidget(button('下载仓资料模板',self.template));self.save_default_btn=button('保存为该仓默认资料',self.save_default);top.addWidget(self.save_default_btn);v.addLayout(top)
        self.sync_btn=button('从聚水潭获取本批仓资料',self.sync_catalog);v.addWidget(self.sync_btn)
        self.source=label('正在读取仓资料…','muted');self.source.setWordWrap(True);v.addWidget(self.source)
        self.order_list=QListWidget();self.order_list.setMaximumHeight(90);self.order_list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        for h in heads:
            item=QListWidgetItem(h['number']+' · '+h.get('name',h['store'])+' · '+h['warehouse']);item.setData(Qt.ItemDataRole.UserRole,h);self.order_list.addItem(item)
        v.addWidget(self.order_list);self.arranging=False;self.order_list.model().rowsMoved.connect(self.order_moved)
        options=QHBoxLayout();options.addWidget(label('分配顺序'))
        self.order_mode=QComboBox();self.order_mode.addItems(['回传确认时间优先','上方人工顺序（拖动后自动采用）']);options.addWidget(self.order_mode)
        self.restore_order_btn=button('恢复回传时间顺序',self.restore_order);options.addWidget(self.restore_order_btn)
        self.order_note=label('默认按回传确认时间；预览后列表显示实际执行顺序。','muted');options.addWidget(self.order_note,1)
        self.correction=QCheckBox('仅替换本批所选未锁单；未选单保留占用');options.addWidget(self.correction);v.addLayout(options);self.order_mode.currentIndexChanged.connect(self.invalidate);self.correction.setChecked(correction);self.correction.setVisible(correction);self.correction.setEnabled(False)
        self.preview_btn=button('预览库存分配',self.preview,True);v.addWidget(self.preview_btn)
        self.summary=label('新批从选用库存开始，不扣其他批占用。预览不保存；确认后固定门店顺序和库存。','muted');self.summary.setWordWrap(True);v.addWidget(self.summary)
        self.review=AllocationReview(self);v.addWidget(self.review,1)
        self.accept_issues=QCheckBox('已核对缺口及待核原因，同意待核商品本次不安排');v.addWidget(self.accept_issues)
        self.auto_export=QCheckBox('每单保存后立即导出，后续继续生成');self.auto_export.setChecked(False);v.addWidget(self.auto_export)
        self.bar=QProgressBar();self.bar.setRange(0,1);v.addWidget(self.bar);self.status=label('待预览','muted');self.status.setWordWrap(True);v.addWidget(self.status)
        actions=QHBoxLayout();self.generate_btn=button('确认生成',self.generate,True);self.generate_btn.setEnabled(False);actions.addWidget(self.generate_btn);self.stop_btn=button('停止剩余任务',self.stop);self.stop_btn.setEnabled(False);actions.addWidget(self.stop_btn);actions.addWidget(button('关闭',self.reject));v.addLayout(actions)
        self.completed=QListWidget();self.completed.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection);self.completed.setMaximumHeight(115);v.addWidget(self.completed)
        self.completed.hide()
        actions=QHBoxLayout();self.completed_note=label('已生成0单 · 已选择0单','muted');actions.addWidget(self.completed_note)
        self.select_completed_btn=button('全选已生成单',self.completed.selectAll);actions.addWidget(self.select_completed_btn)
        self.export_saved_btn=button('导出所选已生成单（0单）',self.export_saved);actions.addWidget(self.export_saved_btn)
        self.lock_saved_btn=button('确认所选已下单并锁定（0单）',self.lock_saved);actions.addWidget(self.lock_saved_btn);v.addLayout(actions)
        self.completed.itemSelectionChanged.connect(self.completed_selection);self.completed_selection()
        self.warehouse.currentIndexChanged.connect(self.describe);self.apply_mode();QTimer.singleShot(0,self.load_catalogs)
    def apply_mode(self):
        for w in (self.update_btn,self.default_btn,self.save_default_btn,self.sync_btn,self.order_mode,self.restore_order_btn,self.order_list):w.setEnabled(not self.batch_id)
        if self.batch_id:self.order_note.setText('沿用本批冻结顺序；不能通过拖动或切换策略改变。')
        self.correction.setEnabled(False)
        self.setAcceptDrops(not self.batch_id)
    def sync_catalog(self):
        if self.worker or self.export_worker or self.batch_id:return
        from catalog_sync_ui import open_sync
        if open_sync(self.host,[self.warehouse.currentText()],'allocation_baseline',self):self.invalidate();self.load_catalogs()
    def load_catalogs(self):
        try:
            if self.batch_id:
                with closing(sqlite3.connect(self.path)) as db:spec=batches.context(db,self.batch_id)
                self.catalogs=spec['stocks']
                self.summary.setText('沿用原批冻结库存、原顺序及已保存占用；更新默认资料不会改变本批。')
            else:self.catalogs=run(self,service.catalogs,(self.path,self.host.repository.folder),'读取可选仓资料')
            self.describe()
        except Exception as exc:warn(self,exc)
    def describe(self):
        c=self.catalogs.get(self.warehouse.currentText())
        if not c:self.source.setText('该仓未提供资料；请导入或采用已保存仓资料。');return
        x=c['catalog'];self.source.setText(f"{x['warehouse']} · {Path(x.get('source_path','')).name} · 导入 {x.get('imported_at','')} · {len(x['items'])}款 · "+('本批冻结依据' if self.batch_id else '待确认采用；资料是否适合本批由业务判断')+'\n'+warehouse_catalog.execution_warning(x))
        if c.get('selection_note'):self.source.setText(self.source.text()+'\n'+c['selection_note'])
        self.source.setToolTip(x.get('source_path','')+'\n库存修订：'+str(x.get('revision',''))+'\n'+c.get('selection_note',''))
    def invalidate(self,*_):
        self.plan=None;self.generate_btn.setEnabled(False);self.summary.setText('资料或顺序变化，请重新预览。')
        self.review.replace([],'原预览已失效，请重新预览')
    def order_moved(self,*_):
        if self.arranging or self.batch_id:return
        self.order_mode.setCurrentIndex(1);self.order_note.setText('已采用上方人工顺序；请重新预览后生成。');self.invalidate()
    def show_order(self,plans,skipped=()):
        by_key={(self.order_list.item(i).data(Qt.ItemDataRole.UserRole)['oid'],self.order_list.item(i).data(Qt.ItemDataRole.UserRole)['warehouse']):self.order_list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.order_list.count())}
        heads=[by_key[(p['head']['oid'],p['head']['warehouse'])] for p in [*plans,*skipped]]
        self.arranging=True
        try:
            self.order_list.clear()
            for h in heads:
                item=QListWidgetItem(h['number']+' · '+h.get('name',h['store'])+' · '+h['warehouse']);item.setData(Qt.ItemDataRole.UserRole,h);self.order_list.addItem(item)
        finally:self.arranging=False
    def restore_order(self):
        if self.batch_id or self.worker:return
        self.order_mode.setCurrentIndex(0);self.preview()
    def completed_selection(self):
        n=len(self.gids());total=self.completed.count();busy=bool(self.worker or self.export_worker)
        self.completed_note.setText(f'已生成{total}单 · 已选择{n}单（高亮选择，可多选）')
        self.export_saved_btn.setText(f'导出所选已生成单（{n}单）');self.lock_saved_btn.setText(f'确认所选已下单并锁定（{n}单）')
        for b in (self.export_saved_btn,self.lock_saved_btn):b.setEnabled(n>0 and not busy)
        self.select_completed_btn.setEnabled(total>0 and not busy)
    def choose(self):
        from PySide6.QtWidgets import QFileDialog
        p,_=QFileDialog.getOpenFileName(self,'更新'+self.warehouse.currentText()+'资料','','Excel (*.xlsx)')
        if p:self.refresh_stock(p)
    def refresh_stock(self,path=None,catalog=None):
        if self.worker or self.batch_id:return
        try:
            wh=self.warehouse.currentText()
            c=run(self,warehouse_catalog.import_catalog,(path,wh),'读取本批仓资料') if path else warehouse_catalog.validate(catalog)
            warning=warehouse_catalog.execution_warning(c)
            if warning:QMessageBox.warning(self,'资料已读取，执行字段待补',warning)
            self.catalogs[wh]={'catalog':c,'bid':None};self.invalidate();self.describe()
        except Exception as exc:warn(self,exc)
    def defaults(self):
        if self.batch_id:return
        try:
            wh=self.warehouse.currentText();c=warehouse_catalog.Repository(self.host.repository.folder).catalogs().get(wh)
            if not c:raise ValueError('请先维护该仓默认商品资料')
            self.refresh_stock(catalog=c)
        except Exception as exc:warn(self,exc)
    def save_default(self):
        c=self.catalogs.get(self.warehouse.currentText())
        if c and confirm(self,'保存为默认仓资料','保存'+self.warehouse.currentText()+'资料供下次使用；历史单据不变。'):
            try:warehouse_catalog.Repository(self.host.repository.folder).save(c['catalog'])
            except Exception as exc:warn(self,exc)
    def template(self):
        import import_templates
        p,_=export_locations.save_file(self,'下载仓资料最低模板','仓商品资料模板.xlsx','Excel (*.xlsx)')
        if p:
            try:import_templates.write('catalog',p)
            except Exception as exc:warn(self,exc)
    def dragEnterEvent(self,event):
        u=event.mimeData().urls()
        if not self.worker and len(u)==1 and u[0].isLocalFile() and Path(u[0].toLocalFile()).suffix.lower()=='.xlsx':event.acceptProposedAction()
    def dropEvent(self,event):self.refresh_stock(event.mimeData().urls()[0].toLocalFile());event.acceptProposedAction()
    def preview(self):
        try:
            heads=[self.order_list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.order_list.count())]
            self.describe()
            if self.batch_id:self.plan=run(self,batches.preview_existing,(self.path,self.batch_id,heads if self.correction_mode else None,self.correction_mode),'核对本批剩余库存')
            else:self.plan=run(self,batches.preview_new,(self.path,heads,{wh:c['catalog'] for wh,c in self.catalogs.items()},self.order_mode.currentIndex()==1),'预览新批库存分配')
            v=self.plan['payload'];self.show_order(v['plans'],v['skipped']);self.review.replace(v['plans'])
            self.order_note.setText('上方列表即本次预览执行顺序；跳过单列在末尾，不参与分配。')
            payment_corrections=sum(bool(p.get('previous_payment')) for p in v['plans'])
            self.summary.setText(f"本次预览 {len(v['plans'])} 张仓单；跳过 {len(v['skipped'])} 张（不计入本次汇总）。"+'；'.join(s['head']['number']+'：'+s['reason'] for s in v['skipped'])+(f'\n其中{payment_corrections}单的旧付款依据已因新回传失效：旧付款事实保留；本次仅更新本地分配，生成后需重新核对付款，不自动更改聚水潭或翱象订单。' if payment_corrections else ''))
            self.generate_btn.setEnabled(bool(v['plans']));self.accept_issues.setChecked(False)
        except Exception as exc:self.plan=None;self.review.replace([],'预览失败，请重新预览');self.generate_btn.setEnabled(False);warn(self,exc)
    def generate(self):
        if not self.plan or self.worker:return
        v=self.plan['payload']
        if any(p['summary']['issues'] for p in v['plans']) and not self.accept_issues.isChecked():QMessageBox.warning(self,'请核对待核商品','请先核对并勾选待核商品本次不安排。');return
        payment_notice='\n旧付款保留；更正分配后请重新核对付款。此处不修改平台订单。' if any(p.get('previous_payment') for p in v['plans']) else ''
        if not confirm(self,'确认生成聚水潭订单',f"将按预览顺序生成{len(v['plans'])}单，保存库存预留与待核对付款草稿；并不表示已经付款。\n"+'\n'.join(p['head']['number']+'：'+p['summary']['amount']+'元' for p in v['plans'])+payment_notice):return
        self.folder=export_locations.initial_directory(self);self.saved=[];self.completed.clear();self.completed.show();self.receipt=None;self.stop_requested=False
        w=Worker(batches.execute,(self.path,self.plan,self.accept_issues.isChecked()),self);self.worker=w
        w.status.connect(self.status.setText);w.row_progress.connect(self.progress);w.commit_state.connect(self.boundary);w.business_event.connect(self.saved_order);w.success.connect(self.generation_done);w.failed.connect(lambda s:warn(self,RuntimeError(s)));w.cancelled.connect(lambda:self.status.setText('已停止，已保存订单保留'));w.finished.connect(self.worker_finished)
        for b in (self.generate_btn,self.preview_btn,self.update_btn,self.default_btn,self.save_default_btn,self.order_mode,self.restore_order_btn,self.correction,self.order_list,self.auto_export,self.sync_btn):b.setEnabled(False)
        self.completed_selection();self.stop_btn.setEnabled(True);w.start()
    def progress(self,n,total):self.bar.show();self.bar.setRange(0,max(1,total) if total else 0);self.bar.setValue(n)
    def boundary(self,on):
        self.committing=on;self.stop_btn.setEnabled(not on);self.stop_btn.setText('正在提交，暂不能停止' if on else '停止剩余任务')
        if not on and self.close_pending:self.stop()
    def saved_order(self,value):
        if not value['positive']:return
        gid=value['gid']
        if gid in self.saved:return
        self.saved.append(gid);item=QListWidgetItem(value['number']+' · 已保存可导出');item.setData(Qt.ItemDataRole.UserRole,gid);self.completed.addItem(item)
        self.completed_selection()
        if self.auto_export.isChecked() and not self.stop_requested:self.export_queue.append(gid);self.dispatch_export()
    def dispatch_export(self):
        if self.export_worker or not self.export_queue:return
        gid=self.export_queue.pop(0);w=Worker(jst_exports.export_checked,(self.path,[gid],self.folder),self);self.export_worker=w
        self.completed_selection()
        w.success.connect(self.export_done);w.failed.connect(lambda s:warn(self,RuntimeError(s)));w.finished.connect(self.export_finished);w.start()
    def export_done(self,r):
        for f in r['files']:
            for i in range(self.completed.count()):
                x=self.completed.item(i)
                if x.data(Qt.ItemDataRole.UserRole)==f['gid']:x.setText(x.text().split(' · ')[0]+' · 已导出')
        if r['failed']:self.status.setText('订单已保存；导出失败，可选中原单重试：'+r['failed'][0]['error'])
        if r.get('warnings'):self.status.setText('；'.join(r['warnings']))
    def export_finished(self):
        w=self.export_worker;self.export_worker=None;w.deleteLater();self.dispatch_export();self.completed_selection()
        if self.close_pending and not self.worker and not self.export_worker:self._completed=True;self.accept()
    def generation_done(self,r):
        plans=self.plan['payload']['plans'] if self.plan else []
        committed=[p for p in plans if p['gid'] in r['generated'] or p['head']['number'] in r['zero']]
        self.review.replace(committed,'本次已保存分配结果（含零分配）')
        self.summary.setText(f"已保存 {len(committed)} / {len(plans)} 张仓单；取消、失败或尚未处理的预览不计入上方结果。")
        self.batch_id=r.get('batch_id',self.batch_id);self.receipt=r;self.status.setText(f"已保存{len(r['generated'])}单，零分配{len(r['zero'])}单，失败/未处理{len(r['errors'])}项，停止：{r['cancelled']}。"+'；'.join(x['error'] for x in r['errors'])+r.get('audit_warning',''))
    def worker_finished(self):
        w=self.worker;self.worker=None;w.deleteLater();self.committing=False;self.stop_btn.setEnabled(False)
        for b in (self.preview_btn,self.update_btn,self.default_btn,self.save_default_btn,self.order_mode,self.restore_order_btn,self.correction,self.order_list,self.auto_export,self.sync_btn):b.setEnabled(True)
        self.apply_mode();self.bar.hide();self.completed_selection()
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
        if not ids:QMessageBox.information(self,'请选择已生成单','在下方完成列表高亮选择要锁定的订单，可多选或点击“全选已生成单”；导出不等于已经下单。');return
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


def generate(parent,host,heads,batch_id=None,correction=False):
    if not heads:QMessageBox.information(parent,'请选择订单','先勾选要生成的店仓订单。');return
    d=GenerationDialog(parent,host,heads,batch_id,correction);previous=getattr(host,'_large_io_dialog',None);host._large_io_dialog=d
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
