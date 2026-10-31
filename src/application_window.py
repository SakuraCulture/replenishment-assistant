"""Home/work/archive shell. Workbench remains a persistent live instance."""
import export_locations
import copy,hashlib,json
from pathlib import Path
from PySide6.QtCore import Qt,QTimer,QSignalBlocker
from PySide6.QtWidgets import QWidget,QVBoxLayout,QHBoxLayout,QStackedWidget,QTableWidget,QTableWidgetItem,QLineEdit,QDialog,QMessageBox,QTextBrowser,QComboBox,QFileDialog,QCheckBox,QInputDialog
from ui_controls import label,button
from workbench import Workbench
import work_repository as wr
import normal_orders,normal_exports,new_products,new_product_focus as focus
import normal_delivery
from new_product_focus_ui import FocusMapping,confirm_focus
from new_product_ui import NewProductDialog
from result_semantics import fmt,order_quantity,order_amount,explanation
from archive_batch_ui import ArchiveBatchActions


class ApplicationWindow(ArchiveBatchActions,Workbench):
    def __init__(self):
        self.new_bases={};self.new_focus=None
        super().__init__()
        self.orders=normal_orders.Repository(self.work_repository)
        from history_tasks import HistoryReader
        self.history_reader=HistoryReader(self.work_repository.path,self.repository.folder/'archive_index.sqlite3',self)
        self.archive_orders=[];self.recent_orders=[];self.archive_page=0;self.archive_all=[];self.archive_matches=[]
        self.status_hashes={};self.status_hash_task=None
        self.archive_timer=QTimer(self);self.archive_timer.setSingleShot(True);self.archive_timer.setInterval(275);self.archive_timer.timeout.connect(self.query_archive)
        self.focus_repository=focus.Repository(self.repository.folder)
        try:self.new_focus=self.focus_repository.load()
        except Exception as exc:self.status.setText("新品关注清单未加载，请重新导入："+str(exc))
        work=self.takeCentralWidget();root=QWidget();v=QVBoxLayout(root);v.setContentsMargins(8,6,8,6);nav=QHBoxLayout();self.top_nav=[]
        for i,title in enumerate(['首页','工作页','订单管理']):
            b=button(title,lambda checked=False,n=i:self.show_section(n));self.top_nav.append(b);nav.addWidget(b)
        nav.addStretch();nav.addWidget(button('导出设置',lambda:export_locations.ExportSettingsDialog(self).exec()));v.addLayout(nav);self.sections=QStackedWidget();v.addWidget(self.sections,1);self.setCentralWidget(root)
        home=QWidget();hv=QVBoxLayout(home);hv.setContentsMargins(36,26,36,26);hv.addWidget(label('今天的补货，从这里开始','title'));self.home_status=label('','section');hv.addWidget(self.home_status)
        hr=QHBoxLayout();hr.addWidget(button('进入工作页',lambda:self.show_section(1),True));hr.addWidget(button('继续已保存的今日草稿',self.resume_today));hr.addWidget(button('查询历史正常单',lambda:self.show_section(2)));hr.addWidget(button('数据维护',self.storage_copy));hv.addLayout(hr)
        hv.addWidget(label('计算后确认归档即签发；门店确认单按仓导出。回传、付款、实际收货在订单生命周期中独立留存。','muted'))
        self.recent=QTableWidget();self.recent.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers);self.recent.cellDoubleClicked.connect(lambda i,c:self.open_archive(self.recent_orders[i]));hv.addWidget(self.recent,1)
        self.recent.hide();hv.removeWidget(self.recent)
        from lifecycle_home_ui import LifecycleWidget as FollowupWidget
        self.followup=FollowupWidget(self);hv.addWidget(self.followup,1)
        self.sections.addWidget(home);self.sections.addWidget(work)
        archive=QWidget();av=QVBoxLayout(archive);av.addWidget(label('订单管理 · 每店每仓独立单据','title'));self.archive_search=QLineEdit();self.archive_search.setPlaceholderText('查询日期 / 门店 / 单号 / 商品编码或名称');self.archive_search.textChanged.connect(self.filter_archive);av.addWidget(self.archive_search)
        filters=QHBoxLayout();self.archive_day=QComboBox();self.archive_day.addItem('全部日期','');self.archive_store=QComboBox();self.archive_store.addItem('全部门店','');filters.addWidget(label('业务日期'));filters.addWidget(self.archive_day);filters.addWidget(label('门店'));filters.addWidget(self.archive_store,1);filters.addWidget(button('清空条件',self.clear_archive_filters));av.addLayout(filters)
        self.archive_warehouse=QComboBox();self.archive_warehouse.addItem('全部仓','');self.archive_warehouse.addItems(['河北仓','山东仓','旧版未标仓']);filters.addWidget(self.archive_warehouse);self.archive_warehouse.currentIndexChanged.connect(self.filter_archive)
        self.archive_trash=QComboBox();self.archive_trash.addItems(['正常归档','回收站']);filters.addWidget(self.archive_trash);self.archive_trash.currentIndexChanged.connect(self.filter_archive)
        self.archive_day.currentIndexChanged.connect(self.filter_archive);self.archive_store.currentIndexChanged.connect(self.filter_archive)
        dates=QHBoxLayout();dates.addWidget(label('日期范围（可留空）'));self.archive_from=QLineEdit();self.archive_to=QLineEdit()
        for field in (self.archive_from,self.archive_to):field.setPlaceholderText('YYYY-MM-DD');field.setMaximumWidth(145);field.textChanged.connect(self.filter_archive)
        dates.addWidget(self.archive_from);dates.addWidget(label('至'));dates.addWidget(self.archive_to);dates.addStretch();av.addLayout(dates)
        self.archive_search.setPlaceholderText('关键词可组合：门店 商品名 / 编码 / 单号（空格分开，回车立即查）');self.archive_search.setClearButtonEnabled(True);self.archive_search.returnPressed.connect(self.refresh_archive)
        from lifecycle_ui import import_group
        av.addWidget(import_group(self,self,refresh=self.refresh_archive))
        av.addWidget(label('数据导出 · 勾选仓单后使用下列独立入口','section'))
        self.setup_archive_exports(av)
        self.archive_table=QTableWidget();self.archive_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers);self.archive_table.cellDoubleClicked.connect(lambda i,c:self.open_archive(self.archive_orders[i]) if c else None);self.archive_table.itemChanged.connect(self.archive_check_changed);av.addWidget(self.archive_table,1)
        self.archive_note=label('','muted');av.addWidget(self.archive_note)
        paging=QHBoxLayout();paging.addWidget(button('上一页',lambda:self.archive_turn(-1)));paging.addWidget(button('下一页',lambda:self.archive_turn(1)));av.addLayout(paging)
        row=QHBoxLayout();row.addWidget(label('单据查看请使用每行的“查看单据”；批量操作仅认勾选。','muted'));row.addWidget(button('查看操作记录',self.show_events));row.addWidget(button('刷新',self.refresh_archive));av.addLayout(row);av.addWidget(label('双击查看：签发快照、历史修订、逐商品数量和来源审计。历史页不改今日工作区。','muted'));self.sections.addWidget(archive)
        self.overview_page.layout().addWidget(label('加盟商用途：制作确认单会先核对签发变化；内部核对文件请使用上方内部明细入口。','muted'));actions=QHBoxLayout();self.new_products_btn=button('选择门店并安排新品',lambda:self.choose_new_products(False));actions.addWidget(self.new_products_btn);self.issue_btn=button('仅确认归档（不导出）',self.confirm_normal);actions.addWidget(self.issue_btn);self.merchant_btn=button('导出门店确认单',self.export_current,True);actions.addWidget(self.merchant_btn);self.overview_page.layout().addLayout(actions)
        self.detail_new_btn=button('安排本店新品',lambda:self.choose_new_products(True));self.pages.widget(4).layout().insertWidget(1,self.detail_new_btn)
        self.mode.addItems(['动销补货','新品候选','新品推荐','缺货恢复及其他','在途衔接风险'])
        self.save_job_btn.setText('保存草稿');self.history_btn.setText('草稿记录');self.batch_export.setText('导出本批内部明细');self.export_all.setText('导出本店全量内部明细');self.export_filtered.setText('导出筛选内部明细')
        focusrow=QHBoxLayout();self.focus_label=label("","muted");focusrow.addWidget(self.focus_label,1);self.focus_import_btn=button("导入新品关注清单",self.import_focus);focusrow.addWidget(self.focus_import_btn);self.focus_empty_btn=button("本月无关注新品",self.empty_focus);focusrow.addWidget(self.focus_empty_btn);self.import_page.layout().addLayout(focusrow)
        self.refresh_focus();self.update_controls();self.show_section(0)
        from usability import install
        install(self,hv)

    def storage_copy(self):
        if self.worker or getattr(self,'_large_io_dialog',None):return
        choice,ok=QInputDialog.getItem(self,'存储维护 · 只生成副本','原数据库保持不变。旧版EXE不能直接读分块记录；可生成旧格式副本用于回退。', ['生成旧格式兼容副本','生成完整分块副本（校验并转换历史）'],0,False)
        if not ok:return
        path,_=export_locations.save_file(self,'保存为新的数据库副本','','SQLite (*.sqlite3)')
        if not path:return
        if Path(path).exists():self.failed('目标文件已存在，请使用新文件名；不会覆盖任何数据库。');return
        from ui_work_io import Session,request
        from runtime_support import Cancelled
        try:
            with Session(self.work_repository.path) as session:receipt=request(self,session,'storage_copy',{'mode':'legacy' if choice.startswith('生成旧格式') else 'upgrade','destination':path})
        except Cancelled:self.status.setText('已取消创建副本，原数据库未改变。');return
        except Exception as exc:self.failed('副本未生成：'+str(exc));return
        QMessageBox.information(self,'副本已生成','已校验并保存：'+receipt['path']+'\n原数据库未改变。回退时请先关闭所有补货助手版本，保留当前库，再将兼容副本复制到目标版本的独立资料目录。新模型结果在旧版中应只读，继续工作请重新计算。')

    def refresh_focus(self):
        if not hasattr(self,'focus_label'):return
        p=self.new_focus
        self.focus_empty_btn.setText('清空本月关注清单' if p and focus.members(p) else '确认本月暂无关注新品')
        self.focus_label.setText(f"新品关注：{p['month']} · {p['name']} · {len(focus.members(p))} 个关注商品"+(' · 月份不符，签发前请更新' if p['month']!=self.job_day[:7] else '') if p else '新品关注：未设置；签发前请导入新品明细，或明确本月无关注新品')

    def import_focus(self):
        if self.worker or self.readonly:return
        from import_ui import choose
        paths=choose(self,'focus','选择独立新品明细');path=paths[0] if paths else None
        if path:self.job(focus.inspect,(path,),self.map_focus)

    def map_focus(self,info):
        if self.closing or (self.worker and self.worker.cancel.is_set()):return
        dialog=FocusMapping(self,info,self.job_day[:7])
        if dialog.exec()!=QDialog.DialogCode.Accepted:return
        # Worker success callbacks run before cleanup; queue the next independent job.
        from PySide6.QtCore import QTimer
        args=(info,dialog.sheet.currentText(),dialog.sku.currentText(),dialog.focus.currentText(),dialog.month.text().strip())
        QTimer.singleShot(0,lambda:self.job(focus.import_list,args,self.apply_focus))

    def empty_focus(self):
        if not self.worker and not self.readonly:self.apply_focus(focus.empty(self.job_day[:7]))

    def apply_focus(self,data):
        if self.closing or (self.worker and self.worker.cancel.is_set()):return
        if not confirm_focus(self,data,self.new_focus):return
        try:self.focus_repository.save(data)
        except Exception as exc:self.failed('关注清单未保存，原清单保留：'+str(exc));return
        self.new_focus=copy.deepcopy(data);self.refresh_focus();self.touch();self.status.setText('新品关注清单已保存；数量不变，本月/往期分组需随正常单重新确认归档。')

    @staticmethod
    def fill_orders(table,records):
        table.setColumnCount(7);table.setHorizontalHeaderLabels(['业务日期','门店','仓','补货单号','修订','签发金额','确认时间']);table.setRowCount(len(records))
        for i,r in enumerate(records):
            for j,val in enumerate([r['day'],r['name']+' ('+r['store']+')',r.get('warehouse','未分仓'),r['number'],f"V{r['revision']:03d}",fmt(r['summary']['amount']),r['saved']]):table.setItem(i,j,QTableWidgetItem(str(val)))
        table.resizeColumnsToContents();table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)

    def show_section(self,index):
        self.sections.setCurrentIndex(index)
        if index in (0,2):self.refresh_archive()
        if index==0 and hasattr(self,'followup'):self.followup.query()

    def refresh_archive(self):
        self.archive_timer.stop();self.query_archive()

    def query_archive(self):
        if self.closing:return
        self.archive_query_pending=True;self.archive_table.setEnabled(False)
        for b in self.archive_export_buttons:b.setEnabled(False)
        self.archive_page=0;self.archive_note.setText('正在读取归档摘要 / 商品索引…（可继续使用工作页）')
        self.home_status.setText('归档摘要加载中；工作区可继续使用。')
        self.history_reader.request({'kind':'list','search':self.archive_search.text(),'day':self.archive_day.currentData() or '', 'from':self.archive_from.text().strip(),'to':self.archive_to.text().strip(), 'store':self.archive_store.currentData() or '', 'warehouse':self.archive_warehouse.currentText() if self.archive_warehouse.currentIndex() else '', 'trash':self.sections.currentIndex()==2 and bool(self.archive_trash.currentIndex())},self.archive_ready,self.archive_failed,
            lambda p:self.archive_note.setText(f"正在读取归档 {p['done']} / {p['total']}；尚未完成的查询不代表无结果"))

    def archive_failed(self,message):
        self.archive_query_pending=False
        self.archive_note.setText('归档未加载：'+message+'；可点击刷新重试');self.home_status.setText('归档未加载，请在历史页刷新重试。')

    def archive_ready(self,data):
        self.archive_all=data['records'];self.archive_matches=data['matches'];self.archive_heads=data['heads']
        from archive_batch import key
        valid={key(h) for h,m in zip(self.archive_all,self.archive_matches) if m}
        self.archive_checked={k:h for k,h in self.archive_checked.items() if k in valid}
        self.archive_query_pending=False;self.archive_table.setEnabled(True)
        for combo,choices in [(self.archive_day,[('全部日期','')]+[(day,day) for day in dict.fromkeys(r['day'] for r in self.archive_all)]),(self.archive_store,[('全部门店','')]+list(dict.fromkeys((r['name']+' ('+r['store']+')',r['store']) for r in self.archive_all)))]:
            selected=combo.currentData();blocker=QSignalBlocker(combo);combo.clear()
            for text,value in choices:combo.addItem(text,value)
            combo.setCurrentIndex(max(0,combo.findData(selected)));del blocker
        self.recent_orders=[r for r in self.archive_all if not r.get('deleted')][:10];self.fill_orders(self.recent,self.recent_orders);self.render_archive_page();self.refresh_home_status()

    def clear_archive_filters(self):
        self.archive_from.clear();self.archive_to.clear()
        self.archive_day.setCurrentIndex(0);self.archive_store.setCurrentIndex(0);self.archive_warehouse.setCurrentIndex(0);self.archive_search.clear();self.refresh_archive()

    def render_archive_page(self):
        matching=[r for r,m in zip(self.archive_all,self.archive_matches) if m];pages=max(1,(len(matching)+99)//100);self.archive_page=max(0,min(self.archive_page,pages-1))
        self.archive_orders=matching[self.archive_page*100:(self.archive_page+1)*100]
        block=QSignalBlocker(self.archive_table);self.fill_orders(self.archive_table,self.archive_orders);self.decorate_archive_selection();del block
        self.archive_note.setText(f'匹配 {len(matching)} / {len(self.archive_all)} 单 · 第 {self.archive_page+1} / {pages} 页（每页100单）'+(' · 无匹配，可清空条件重查' if not matching else ' · 双击单据，再点选商品查看当时依据'))

    def archive_turn(self,offset):
        self.archive_page+=offset;self.render_archive_page()

    def filter_archive(self,*_):
        if hasattr(self,'archive_checked'):
            self.archive_checked.clear();self.render_archive_page();self.archive_selection_note.setText('查询条件已变化，已清空勾选；请在新结果中选择')
        self.archive_note.setText('等待查询…');self.archive_timer.start()

    def refresh_home_status(self):
        heads={(r['day'],r['store']):r for r in getattr(self,'archive_heads',[])}
        today=[r for r in self.archive_all if r['day']==wr.today() and not r.get('deleted')];ready=[s for s,x in self.states.items() if x['state']=='已完成']
        missing={s:self.results[s] for s in ready if heads.get((self.job_day,s)) and not self.valid_status_hash(s)}
        pending=sum(not heads.get((self.job_day,s)) or (self.valid_status_hash(s) and self.status_hashes[s][2]!=heads[(self.job_day,s)]['digest']) for s in ready)
        self.home_status.setText(f"{wr.today()} · 今日已归档 {len({r['store'] for r in today})} 店 / {len(today)} 仓单 / ¥{sum(r['summary']['amount'] for r in today):,.2f}\n工作区日期 {self.job_day} · 待确认 {pending} 店"+(f' · 正在核对 {len(missing)} 店签发状态' if missing else '')+f" · 未完成/需处理 {len(self.states)-len(ready)} 店 · {'任务运行中' if self.worker else '有未保存草稿' if self.job_dirty else '工作区就绪'}")
        self.status_hashes={s:v for s,v in self.status_hashes.items() if s in self.results and v[0] is self.results[s] and v[1] is self.new_focus}
        if missing and self.status_hash_task is None:
            from history_tasks import StatusHashes
            task=StatusHashes(missing,self.new_focus,self);self.status_hash_task=task
            def ready_hashes(values):
                for s,h in values.items():
                    if self.results.get(s) is task.items[s] and self.new_focus is task.focus:self.status_hashes[s]=(task.items[s],task.focus,h)
            def finished():
                self.status_hash_task=None;task.deleteLater()
                if not self.closing:self.refresh_home_status()
            task.completed.connect(ready_hashes);task.finished.connect(finished);task.start()

    def valid_status_hash(self,s):
        cached=self.status_hashes.get(s)
        return bool(cached and cached[0] is self.results.get(s) and cached[1] is self.new_focus)

    def closeEvent(self,event):
        super().closeEvent(event)
        if event.isAccepted():
            self.archive_timer.stop();self.history_reader.shutdown()
            self.followup.shutdown()
            self.summary_pending=None
            if self.summary_task:self.summary_task.wait()
            if self.status_hash_task:
                self.status_hash_task.requestInterruption();self.status_hash_task.wait()

    def resume_today(self):
        if self.worker:return
        if not any(r['day']==wr.today() for r in self.work_repository.list_jobs()):self.show_section(1);self.status.setText('今天暂无保存草稿，请准备数据。');return
        if not self.leave_current():return
        from ui_work_io import read_job
        from runtime_support import Cancelled
        try:record=read_job(self,self.work_repository.path,wr.today())
        except Cancelled:self.status.setText('已取消读取作业。');return
        except Exception as exc:self.failed('作业未打开：'+str(exc));return
        if record is None:self.failed('今天的记录已不存在，请刷新后重试。');return
        self.restore_job(record,owned=True);self.show_section(1)

    def update_controls(self):
        super().update_controls()
        if not hasattr(self,'issue_btn'):return
        ready=not self.worker and not self.readonly and not self.input_dirty
        if hasattr(self,'focus_import_btn'):
            self.focus_import_btn.setEnabled(not self.worker and not self.readonly);self.focus_empty_btn.setEnabled(not self.worker and not self.readonly)
        self.issue_btn.setEnabled(ready and self.job_day==wr.today() and any(x['state']=='已完成' for x in self.states.values()))
        self.new_products_btn.setEnabled(ready and any(x['state']=='已完成' for x in self.states.values()))
        self.merchant_btn.setEnabled(not self.worker and not self.input_dirty and any(x['state']=='已完成' for x in self.states.values()))

    def snapshot(self):
        p=super().snapshot()
        if self.new_focus is not None:p["new_focus"]=copy.deepcopy(self.new_focus)
        if self.new_bases:p['new_product_bases']=dict(self.new_bases)
        return p

    def restore_job(self,record,owned=False):
        clone=(lambda value:value) if owned else copy.deepcopy
        self.new_bases=clone(record['payload'].get('new_product_bases',{}));self.new_focus=clone(record['payload'].get('new_focus'));super().restore_job(record,owned=owned);self.refresh_focus()

    def reset_job(self):
        self.new_bases={};super().reset_job()
        try:self.new_focus=self.focus_repository.load()
        except Exception as exc:self.new_focus=None;self.status.setText("新品关注清单未加载："+str(exc))
        self.refresh_focus()
    def loaded_ok(self,loaded):self.new_bases={};super().loaded_ok(loaded)
    def task_changed(self,store,state,result,error):
        if result:self.new_bases.pop(store,None)
        super().task_changed(store,state,result,error)

    def start_calculation(self,codes=None):
        targets=codes if isinstance(codes,list) else self.selected_codes()
        chosen=[s for s in targets if self.results.get(s,{}).get('new_products')]
        if chosen and not self.worker:
            box=QMessageBox(self);box.setObjectName('newSelectionReset');box.setWindowTitle('重新计算前确认新品选择');box.setText('以下门店重新计算后将清空本次新品选择，请按新数据和参数重新选品；已归档单据保持原修订。\n'+'、'.join(chosen));cancel=box.addButton('取消',QMessageBox.ButtonRole.RejectRole);accept=box.addButton('重新计算并重选新品',QMessageBox.ButtonRole.AcceptRole);box.setDefaultButton(cancel);box.exec()
            if box.clickedButton() is not accept:return
        super().start_calculation(codes)

    def pick_store(self,use_current=False):
        codes=[s for s,x in self.states.items() if x['state']=='已完成']
        if not codes:return None
        if use_current and self.current_store in codes:return self.current_store
        names=[self.states[s]['name']+' ('+s+')' for s in codes];choice,ok=QInputDialog.getItem(self,'选择门店','本次操作门店',names,0,False)
        return codes[names.index(choice)] if ok else None

    def choose_new_products(self,use_current=False):
        if self.worker or self.readonly or self.input_dirty:return
        s=self.pick_store(use_current)
        if not s:return
        current=self.results[s];base=self.new_bases.get(s,current)
        if base.get('new_products'):
            self.failed('当前草稿缺少新品原计算快照，请重新计算该店后选择。');return
        dialog=NewProductDialog(self,base,current,self.new_focus)
        accepted=dialog.exec()==QDialog.DialogCode.Accepted
        selections=dialog.selections() if accepted else None
        dialog.deleteLater()
        if not accepted:return
        self.job(new_products.apply,(base,selections),lambda result:self.preview_new(s,base,current,result))

    def preview_new(self,s,base,current,result):
        if self.closing or (self.worker and self.worker.cancel.is_set()):return
        diffs=new_products.changes(current,result);box=QMessageBox(self);box.setObjectName('newBudgetPreview');box.setWindowTitle('预览预算变化 · '+result['store_name']);box.setText(f"调整 {len(diffs)} 款；本店安排 ¥{current['summary']['amount']:,.2f} → ¥{result['summary']['amount']:,.2f}\n最低持有缺口 {current['summary']['holding_gaps']} → {result['summary']['holding_gaps']}\n必卖新品可占用普通动销的预算。取消保持原方案；应用后需重新确认归档。");box.setDetailedText(json.dumps(diffs,ensure_ascii=False,indent=2));cancel=box.addButton('取消',QMessageBox.ButtonRole.RejectRole);apply=box.addButton('应用到本店草稿',QMessageBox.ButtonRole.AcceptRole);box.setDefaultButton(cancel);box.exec()
        if box.clickedButton() is not apply:return
        # Published result trees are replaced, never mutated; apply() copies its input.
        if result.get('new_products'):self.new_bases[s]=base
        else:self.new_bases.pop(s,None)
        self.results[s]=result;self.current_store=s;self.result=result;self.dirty=False;self.set_rows(result['rows'],True);self.refresh_tables();self.touch();self.navigate(4)
        self.status.setText('新品选择与预算已应用到本店草稿；请返回门店汇总选择门店并导出。')

    def confirm_normal(self):
        if self.worker or self.readonly or self.input_dirty or self.job_day!=wr.today():return
        from interaction_ui import choose_stores
        codes=choose_stores(self,'选择需要确认归档的门店',self.states,self.selected_codes())
        if not codes:self.failed('请先在门店页勾选有效完成门店。');return
        from ui_work_io import Session,request
        from runtime_support import Cancelled
        try:
            with Session(self.work_repository.path) as session:
                prepared=request(self,session,'prepare_normal',[self.snapshot(),codes])
                box=QMessageBox(self);box.setObjectName('normalConfirm');box.setWindowTitle('确认归档本次补货建议');box.setText('内部签发确认；不代表加盟商确认或已下单。\n同时保存当前完整草稿；只更新下列门店正常单，其他门店历史单保留。')
                box.setInformativeText('\n'.join(prepared['lines']));box.setDetailedText(prepared['detail_text']+('\n…差异较多，此处显示前200条。点击“导出完整差异”查看全部内容，未删减任何归档数据。' if prepared['truncated'] else ''))
                cancel=box.addButton('取消',QMessageBox.ButtonRole.RejectRole);accept=box.addButton(f'确认归档 {len(codes)} 家店',QMessageBox.ButtonRole.AcceptRole);review=box.addButton('导出完整差异',QMessageBox.ButtonRole.ActionRole);box.setDefaultButton(cancel);box.setEscapeButton(cancel)
                while True:
                    box.exec()
                    if box.clickedButton() is not review:break
                    path,_=export_locations.save_file(self,'保存完整签发差异','签发差异.xlsx','Excel (*.xlsx)')
                    if path:
                        if not path.lower().endswith('.xlsx'):path+='.xlsx'
                        request(self,session,'review_export',path)
                if box.clickedButton() is not accept:return
                receipt=request(self,session,'confirm_normal',None)
            self.saved_time=receipt['saved'];self.job_dirty=False;self.refresh_job_label();self.refresh_archive();self.status.setText('已自动归档：'+'；'.join(r['number']+f" V{r['revision']:03d}" for r in receipt['orders'])+'。可导出门店确认单。')
        except Cancelled:self.status.setText('已取消核对，原正常单与草稿保留。')
        except Exception as exc:self.failed('归档操作未完成，请在历史记录核对状态：'+str(exc))

    def export_current(self,codes=None):
        if self.worker or self.input_dirty:return
        warehouses=None
        if not isinstance(codes,list):
            from merchant_selection import MerchantSelection
            dialog=MerchantSelection(self)
            if dialog.exec()!=QDialog.DialogCode.Accepted:return
            codes=dialog.codes();warehouses=dialog.warehouses()
        if not codes:return
        from ui_work_io import Session,request
        from runtime_support import Cancelled
        try:
            with Session(self.work_repository.path) as session:
                full=self.snapshot()
                slim={k:full[k] for k in ('day','input_dirty','new_focus')}
                slim.update(states={s:full['states'][s] for s in codes},results={s:full['results'][s] for s in codes},model_version=full['model_version'],_draft_summary=wr.summary(full))
                prepared=request(self,session,'delivery_probe',[slim,codes,warehouses]);plans=prepared['plans']
                if any(not p['same'] for p in plans):
                    tail={**full,'results':{s:r for s,r in full['results'].items() if s not in codes}}
                    request(self,session,'delivery_expand',[tail,list(full['results'])])
                    box=QMessageBox(self);box.setObjectName('deliveryConfirm');box.setWindowTitle('确认签发并批量导出门店确认单');lines=[]
                    for p in plans:
                        old=p['old'];action='沿用已归档修订' if p['same'] else f"覆盖当天正常单 V{old['revision']:03d} → V{old['revision']+1:03d}（旧版保留）" if old else '首次签发 V001'
                        lines.append(p['name']+' ('+p['store']+')：'+action+f"；¥{p['amount']:,.2f}")
                    box.setText(f'草稿保存范围：当前完整每日草稿。\n正常单签发范围：所选 {len(plans)} 家店的完整正常单（包括未选择导出的仓）。\n文件输出范围：已选 {len(plans)} 家门店，共 {sum(len(p["files"]) for p in plans)} 份分仓确认单。仅导出所选仓；归档仍保存各店完整正常单。\n'+'\n'.join(lines)+'\n确认后自动归档并逐店导出；不是实际下单。')
                    detail={'所选门店':[p['store'] for p in plans]}
                    if prepared['head']:
                        detail['同日草稿覆盖']={'原保存时间':prepared['head']['saved'],'原门店状态':prepared['head']['summary'],'本次门店状态':prepared['summary']};box.setInformativeText('同时覆盖当天完整草稿；未选门店正常单不变。展开详情查看草稿覆盖范围。')
                    from history_forms import fields
                    draft_text='\n'.join(k+'：'+v for k,v,t in fields(detail))
                    box.setDetailedText(prepared['detail_text']+'\n\n草稿覆盖范围：\n'+draft_text);box.setInformativeText('确认时同时保存完整草稿；未选门店正常单不变。按所选门店顺序列出商品、参数和来源变化。'+('明细已截断，请导出完整差异Excel。' if prepared['truncated'] else '展开详情核对；可导出完整差异Excel。'));save_diff=box.addButton('导出完整差异Excel',QMessageBox.ButtonRole.ActionRole);cancel=box.addButton('取消',QMessageBox.ButtonRole.RejectRole);accept=box.addButton('确认归档并导出',QMessageBox.ButtonRole.AcceptRole);box.setDefaultButton(cancel);box.setEscapeButton(cancel)
                    while True:
                        box.exec()
                        if box.clickedButton() is not save_diff:break
                        diff_path,_=export_locations.save_file(self,'保存完整签发差异','签发差异.xlsx','Excel (*.xlsx)')
                        if diff_path:request(self,session,'review_export',diff_path if diff_path.lower().endswith('.xlsx') else diff_path+'.xlsx')
                    if box.clickedButton() is not accept:return
                else:
                    from interaction_ui import confirm
                    if not confirm(self,'导出已签发版本','不新增签发修订；按以下原修订生成文件：\n'+'\n'.join(p['name']+f" · V{p['old']['revision']:03d} · {len(p['files'])}份" for p in plans)):return
                files=[f for p in plans for f in p['files']]
                if len(files)==1:
                    path,_=export_locations.save_file(self,'导出本仓门店确认单',files[0]['filename'],'Excel (*.xlsx)')
                    if path and not path.lower().endswith('.xlsx'):path+='.xlsx'
                else:path=export_locations.directory(self,'选择批量确认单导出文件夹')
                if not path:return
                receipt=request(self,session,'delivery_execute',path)
            self.delivery_done(receipt)
        except Cancelled:self.status.setText('已取消导出核对，原正常单与草稿保留。')
        except Exception as exc:self.failed('导出操作未全部完成：'+str(exc))

    def delivery_done(self,receipt):
        if receipt['signed']:
            self.saved_time=receipt['saved'];self.job_dirty=False;self.refresh_job_label()
        self.status.setText(f"已导出 {len(receipt['paths'])} 份分仓确认单；签发来源已归档。"+'；'.join(receipt['paths']))

    def export_order(self,order):
        if self.worker:return
        from history_read import read_for_export
        self.job(read_for_export,(self.orders,order),lambda loaded:self.export_loaded_order({**loaded,'selected_warehouse':order.get('warehouse')}))

    def export_loaded_order(self,order):
        from merchant_selection import MerchantSelection
        from normal_delivery import export_signed
        if order.get('selected_warehouse'):
            warehouses=[order['selected_warehouse']]
        else:
            dialog=MerchantSelection(self,order)
            if dialog.exec()!=QDialog.DialogCode.Accepted:return
            warehouses=dialog.warehouses().get(order['store'],[])
        if not warehouses:return
        if len(warehouses)==1:
            path,_=export_locations.save_file(self,'导出本仓门店确认单',normal_exports.filename(order,warehouses[0]),'Excel (*.xlsx)')
            if path and not path.lower().endswith('.xlsx'):path+='.xlsx'
        else:path=export_locations.directory(self,'选择分仓确认单导出文件夹')
        if not path:return
        def done(paths):self.status.setText('已导出 '+'；'.join(paths)+f'；原归档 V{order["revision"]:03d} 不变。')
        def start():
            if self.closing:return
            if self.worker:QTimer.singleShot(10,start);return
            self.job(export_signed,(self.orders,order,path,warehouses),done)
        QTimer.singleShot(0,start)

    def export_archive(self,head,kind='calculation'):
        self.export_archive_batch(kind,[head])

    def export_selected_archive(self):
        i=self.archive_table.currentRow()
        if i>=0:self.export_archive(self.archive_orders[i])
        else:QMessageBox.information(self,'请选择仓单','先点选一条仓单，再导出。')

    def change_archive_visibility(self,deleted=None):
        from archive_orders import change_many
        from business_ui import run
        if deleted is None:deleted=not bool(self.archive_trash.currentIndex())
        heads=list(self.archive_checked.values())
        if not heads or getattr(self,'archive_query_pending',False):return
        if any(bool(h.get('deleted'))==bool(deleted) for h in heads):
            QMessageBox.warning(self,'请重新勾选','勾选包含不适用当前操作的仓单，请刷新后重选。');return
        action='移入回收站' if deleted else '恢复归档'
        from interaction_ui import confirm
        details='\n'.join(f"{h['day']} · {h['store']} · {h['warehouse']} · {h['number']} · V{h['revision']:03d}" for h in heads)
        message=f'{action} {len(heads)} 张勾选仓单（包括各自全部修订）。\n'+details+'\n另一仓、原数量金额、回传和原文件保留。整批执行，状态发生变化时整批拒绝。'
        if not confirm(self,action,message):return
        try:
            run(self,change_many,(self.work_repository.path,heads,deleted),action,False)
            self.archive_checked.clear();self.refresh_archive()
        except Exception as exc:QMessageBox.warning(self,'未完成',str(exc))

    def open_selected_archive(self):
        i=self.archive_table.currentRow()
        if i>=0:self.open_archive(self.archive_orders[i])

    def open_archive(self,record):
        from history_ui import ArchiveDialog
        dialog=ArchiveDialog(self,record);dialog.exec();dialog.dispose();dialog.deleteLater()
        if not self.closing:self.refresh_archive()

    def import_merchant_returns(self):
        from feedback_ui import import_returns
        import_returns(self,self)

    def show_events(self,oid=None,warehouse=None):
        from history_ui import EventsDialog
        from business_ui import run
        from history_forms import events
        from runtime_support import Cancelled
        # QPushButton.clicked's bool is not an order id.
        try:data=run(self,events,(self.work_repository.path,None if isinstance(oid,bool) else oid,warehouse),'查询归档日志')
        except Cancelled:return
        except Exception as exc:self.failed(str(exc));return
        dialog=EventsDialog(self,data['records'],data['rows']);dialog.exec();dialog.deleteLater()
