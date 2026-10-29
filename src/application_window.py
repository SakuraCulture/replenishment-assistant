"""Home/work/archive shell. Workbench remains a persistent live instance."""
import copy,hashlib,json
from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget,QVBoxLayout,QHBoxLayout,QStackedWidget,QTableWidget,QTableWidgetItem,QLineEdit,QDialog,QMessageBox,QTextBrowser,QComboBox,QFileDialog,QCheckBox,QInputDialog
from ui_controls import label,button
from workbench import Workbench
import work_repository as wr
import normal_orders,normal_exports,new_products,new_product_focus as focus
from new_product_focus_ui import FocusMapping,confirm_focus
from new_product_ui import NewProductDialog
from result_semantics import fmt,order_quantity,order_amount,explanation


class ApplicationWindow(Workbench):
    def __init__(self):
        self.new_bases={};self.new_focus=None
        super().__init__()
        self.orders=normal_orders.Repository(self.work_repository)
        self.focus_repository=focus.Repository(self.repository.folder)
        try:self.new_focus=self.focus_repository.load()
        except Exception as exc:self.status.setText("新品关注清单未加载，请重新导入："+str(exc))
        work=self.takeCentralWidget();root=QWidget();v=QVBoxLayout(root);v.setContentsMargins(8,6,8,6);nav=QHBoxLayout();self.top_nav=[]
        for i,title in enumerate(['首页','工作页','历史归档 / 日志']):
            b=button(title,lambda checked=False,n=i:self.show_section(n));self.top_nav.append(b);nav.addWidget(b)
        nav.addStretch();v.addLayout(nav);self.sections=QStackedWidget();v.addWidget(self.sections,1);self.setCentralWidget(root)
        home=QWidget();hv=QVBoxLayout(home);hv.setContentsMargins(36,26,36,26);hv.addWidget(label('今天的补货，从这里开始','title'));self.home_status=label('','section');hv.addWidget(self.home_status)
        hr=QHBoxLayout();hr.addWidget(button('开始 / 继续工作',lambda:self.show_section(1),True));hr.addWidget(button('继续已保存的今日草稿',self.resume_today));hr.addWidget(button('查询历史正常单',lambda:self.show_section(2)));hv.addLayout(hr)
        hv.addWidget(label('保存草稿用于续做；确认归档签发正常单；正式导出供加盟商核对。每天每店仅一张当前正常单。','muted'))
        self.recent=QTableWidget();self.recent.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers);self.recent.cellDoubleClicked.connect(lambda i,c:self.open_archive(self.recent_orders[i]));hv.addWidget(self.recent,1)
        hv.addWidget(label('确认回传、实际下单、收货表的历史关联将在后续独立接入；当前不会自动发送或下单。','muted'))
        self.sections.addWidget(home);self.sections.addWidget(work)
        archive=QWidget();av=QVBoxLayout(archive);av.addWidget(label('历史正常单 · 当前修订去重统计','title'));self.archive_search=QLineEdit();self.archive_search.setPlaceholderText('查询日期 / 门店 / 单号 / 商品编码或名称');self.archive_search.textChanged.connect(self.filter_archive);av.addWidget(self.archive_search)
        self.archive_table=QTableWidget();self.archive_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers);self.archive_table.cellDoubleClicked.connect(lambda i,c:self.open_archive(self.archive_orders[i]));av.addWidget(self.archive_table,1)
        row=QHBoxLayout();row.addWidget(button('打开选中单据 / 修订',self.open_selected_archive));row.addWidget(button('操作日志明细 / 导出',self.show_events));row.addWidget(button('刷新',self.refresh_archive));av.addLayout(row);av.addWidget(label('双击查看：签发快照、历史修订、逐商品数量和来源审计。历史页不改今日工作区。','muted'));self.sections.addWidget(archive)
        actions=QHBoxLayout();self.new_products_btn=button('选择本店新品 / 预览预算',self.choose_new_products);actions.addWidget(self.new_products_btn);self.issue_btn=button('确认所选门店并自动归档',self.confirm_normal,True);actions.addWidget(self.issue_btn);self.merchant_btn=button('导出加盟商确认单（带号）',self.export_current);actions.addWidget(self.merchant_btn);self.overview_page.layout().addLayout(actions)
        self.pages.widget(4).layout().insertWidget(1,button('选择本店新品 / 预览预算',self.choose_new_products))
        self.mode.addItems(['动销补货','新品候选','新品推荐','缺货恢复及其他'])
        self.save_job_btn.setText('保存草稿');self.history_btn.setText('草稿记录');self.batch_export.setText('内部批次明细导出');self.export_all.setText('内部全量明细');self.export_filtered.setText('内部当前筛选')
        focusrow=QHBoxLayout();self.focus_label=label("","muted");focusrow.addWidget(self.focus_label,1);self.focus_import_btn=button("导入新品明细 / 关注字段",self.import_focus);focusrow.addWidget(self.focus_import_btn);self.focus_empty_btn=button("本月无关注新品",self.empty_focus);focusrow.addWidget(self.focus_empty_btn);self.import_page.layout().addLayout(focusrow)
        self.refresh_focus();self.update_controls();self.show_section(0)

    def refresh_focus(self):
        if not hasattr(self,'focus_label'):return
        p=self.new_focus
        self.focus_label.setText(f"新品关注：{p['month']} · {p['name']} · {len(focus.members(p))} 个关注商品"+(' · 月份不符，签发前请更新' if p['month']!=self.job_day[:7] else '') if p else '新品关注：未设置；签发前请导入新品明细，或明确本月无关注新品')

    def import_focus(self):
        if self.worker or self.readonly:return
        path,_=QFileDialog.getOpenFileName(self,'选择独立新品明细','','Excel (*.xlsx)')
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
        table.setColumnCount(6);table.setHorizontalHeaderLabels(['业务日期','门店','补货单号','修订','签发金额','确认时间']);table.setRowCount(len(records))
        for i,r in enumerate(records):
            for j,val in enumerate([r['day'],r['name']+' ('+r['store']+')',r['number'],f"V{r['revision']:03d}",fmt(r['summary']['amount']),r['saved']]):table.setItem(i,j,QTableWidgetItem(str(val)))
        table.resizeColumnsToContents();table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)

    def show_section(self,index):
        self.sections.setCurrentIndex(index)
        if index in (0,2):self.refresh_archive()

    def refresh_archive(self):
        self.archive_orders=self.orders.list();self.fill_orders(self.archive_table,self.archive_orders);self.filter_archive()
        self.recent_orders=self.archive_orders[:10];self.fill_orders(self.recent,self.recent_orders)
        today=[r for r in self.archive_orders if r['day']==wr.today()];ready=[s for s,x in self.states.items() if x['state']=='已完成'];pending=sum(not self.orders.get(self.job_day,s) or self.orders.get(self.job_day,s)['digest']!=normal_orders.fingerprint(self.results[s],self.new_focus) for s in ready)
        self.home_status.setText(f"{wr.today()} · 今日已归档 {len(today)} 店 / ¥{sum(r['summary']['amount'] for r in today):,.2f}\n工作区日期 {self.job_day} · 待确认 {pending} 店 · 未完成/需处理 {len(self.states)-len(ready)} 店 · {'任务运行中' if self.worker else '有未保存草稿' if self.job_dirty else '工作区就绪'}")

    def filter_archive(self,*_):
        text=self.archive_search.text().strip().lower()
        for i,r in enumerate(getattr(self,'archive_orders',[])):
            match=text in json.dumps(r,ensure_ascii=False).lower()
            if text and not match:
                full=self.orders.get(r['day'],r['store']);match=any(text in (d['E']+' '+str(d.get('D',''))).lower() for d in full['payload']['result']['rows'])
            self.archive_table.setRowHidden(i,not match)

    def resume_today(self):
        if self.worker:return
        record=self.work_repository.get_job(wr.today())
        if not record:self.show_section(1);self.status.setText('今天暂无保存草稿，请准备数据。');return
        if self.leave_current():self.restore_job(record);self.show_section(1)

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
        if self.new_bases:p['new_product_bases']=copy.deepcopy(self.new_bases)
        return p

    def restore_job(self,record):
        self.new_bases=copy.deepcopy(record['payload'].get('new_product_bases',{}));self.new_focus=copy.deepcopy(record['payload'].get('new_focus'));super().restore_job(record);self.refresh_focus()

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

    def pick_store(self):
        codes=[s for s,x in self.states.items() if x['state']=='已完成']
        if not codes:return None
        if self.current_store in codes:return self.current_store
        names=[self.states[s]['name']+' ('+s+')' for s in codes];choice,ok=QInputDialog.getItem(self,'选择门店','本次操作门店',names,0,False)
        return codes[names.index(choice)] if ok else None

    def choose_new_products(self):
        if self.worker or self.readonly or self.input_dirty:return
        s=self.pick_store()
        if not s:return
        current=self.results[s];base=self.new_bases.get(s,current)
        if base.get('new_products'):
            self.failed('当前草稿缺少新品原计算快照，请重新计算该店后选择。');return
        dialog=NewProductDialog(self,base,current,self.new_focus)
        if dialog.exec()!=QDialog.DialogCode.Accepted:return
        selections=dialog.selections()
        self.job(new_products.apply,(base,selections),lambda result:self.preview_new(s,base,current,result))

    def preview_new(self,s,base,current,result):
        if self.closing or (self.worker and self.worker.cancel.is_set()):return
        diffs=new_products.changes(current,result);box=QMessageBox(self);box.setObjectName('newBudgetPreview');box.setWindowTitle('预览预算变化 · '+result['store_name']);box.setText(f"调整 {len(diffs)} 款；本店安排 ¥{current['summary']['amount']:,.2f} → ¥{result['summary']['amount']:,.2f}\n最低持有缺口 {current['summary']['holding_gaps']} → {result['summary']['holding_gaps']}\n必卖新品可占用普通动销的预算。取消保持原方案；应用后需重新确认归档。");box.setDetailedText(json.dumps(diffs,ensure_ascii=False,indent=2));cancel=box.addButton('取消',QMessageBox.ButtonRole.RejectRole);apply=box.addButton('应用到本店草稿',QMessageBox.ButtonRole.AcceptRole);box.setDefaultButton(cancel);box.exec()
        if box.clickedButton() is not apply:return
        if result.get('new_products'):self.new_bases[s]=copy.deepcopy(base)
        else:self.new_bases.pop(s,None)
        self.results[s]=result;self.current_store=s;self.result=result;self.dirty=False;self.set_rows(result['rows'],True);self.refresh_tables();self.touch();self.navigate(4)
        self.status.setText('新品选择与预算已应用到本店草稿；请返回门店汇总确认归档。')

    def confirm_normal(self):
        if self.worker or self.readonly or self.input_dirty or self.job_day!=wr.today():return
        codes=[s for s in self.selected_codes() if self.states[s]['state']=='已完成']
        if not codes:self.failed('请先在门店页勾选有效完成门店。');return
        try:focus.require_for(self.new_focus,self.job_day,[self.results[s] for s in codes])
        except ValueError as exc:self.failed(str(exc));return
        payload=copy.deepcopy(self.snapshot());oldjob=self.work_repository.get_job(self.job_day);old={s:self.orders.get(self.job_day,s) for s in codes}
        box=QMessageBox(self);box.setObjectName('normalConfirm');box.setWindowTitle('确认归档本次补货建议');box.setText('内部签发确认；不代表加盟商确认或已下单。\n同时保存当前完整草稿；只更新下列门店正常单，其他门店历史单保留。')
        lines=[];details={}
        for s in codes:
            r=self.results[s];prev=old[s];same=prev and prev['digest']==normal_orders.fingerprint(r,self.new_focus)
            lines.append(r['store_name']+' ('+s+')：'+('已归档，无需新修订' if same else f"同号 V{prev['revision']:03d} → V{prev['revision']+1:03d}" if prev else '首次 V001')+f"；¥{r['summary']['amount']:,.2f}，{r['summary']['positive']}款，待核{r['summary']['issues']}，预算缺口{r['summary']['budget_deferred']}")
            details[s]={'关注清单原版本':(prev['payload'].get('new_focus') or {}).get('revision') if prev else None,'关注清单新版本':(self.new_focus or {}).get('revision'),'new_selection':r.get('new_products',{}),'changes':normal_orders.revision_diff(prev['payload']['result'] if prev else None,r),'before_amount':prev['payload']['result']['summary']['amount'] if prev else None,'after_amount':r['summary']['amount']}
        if oldjob:details['草稿覆盖']={'原保存时间':oldjob['saved'],'原门店状态':oldjob['summary']['stores'],'新门店状态':wr.summary(payload)['stores']}
        box.setInformativeText('\n'.join(lines));box.setDetailedText(json.dumps(details,ensure_ascii=False,indent=2));cancel=box.addButton('取消',QMessageBox.ButtonRole.RejectRole);accept=box.addButton('确认并归档',QMessageBox.ButtonRole.AcceptRole);box.setDefaultButton(cancel);box.setEscapeButton(cancel);box.exec()
        if box.clickedButton() is not accept:return
        try:
            issued=self.orders.confirm(payload,codes,oldjob['revision'] if oldjob else None,{s:r['revision'] if r else None for s,r in old.items()})
            saved=self.work_repository.get_job(self.job_day);self.saved_time=saved['saved'];self.job_dirty=False;self.refresh_job_label();self.refresh_archive();self.status.setText('已自动归档：'+'；'.join(r['number']+f" V{r['revision']:03d}" for r in issued)+'。可导出加盟商确认单。')
        except Exception as exc:self.failed('归档失败，原正常单与草稿保留：'+str(exc))

    def export_current(self):
        if self.worker or self.input_dirty:return
        s=self.pick_store()
        if not s:return
        order=self.orders.get(self.job_day,s)
        if not order or order['digest']!=normal_orders.fingerprint(self.results[s],self.new_focus):self.failed('当前方案尚未确认归档，或与签发版本不同；请先确认归档。旧版本可从历史页单独导出。');return
        self.export_order(order)

    def export_order(self,order):
        if self.worker:return
        order=self.orders.get(order['day'],order['store'],order['revision'])
        name=normal_exports.filename(order)
        path,_=QFileDialog.getSaveFileName(self,'导出本店加盟商确认单',name,'Excel (*.xlsx)')
        if not path:return
        if not path.lower().endswith('.xlsx'):path+='.xlsx'
        def done(path):
            try:self.orders.log(order,'带号导出',{'path':path,'sha256':hashlib.sha256(Path(path).read_bytes()).hexdigest()})
            except Exception as exc:self.failed('文件已导出，但导出日志未保存：'+str(exc));return
            self.status.setText('已导出 '+path+'；归档版本不变。')
        self.job(normal_exports.export,(copy.deepcopy(order),path),done)

    def open_selected_archive(self):
        i=self.archive_table.currentRow()
        if i>=0:self.open_archive(self.archive_orders[i])

    def open_archive(self,record):
        dialog=QDialog(self);dialog.setWindowTitle(record['number']+' · 历史签发快照');dialog.resize(1100,730);v=QVBoxLayout(dialog);rev=QComboBox();rev.addItems([f'V{i:03d}'+(' · 当前' if i==record['revision'] else ' · 历史') for i in range(1,record['revision']+1)]);v.addWidget(rev);note=label('','muted');v.addWidget(note);search=QLineEdit();search.setPlaceholderText('筛选商品名称 / 编码（不影响正式导出范围）');v.addWidget(search);table=QTableWidget();table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers);v.addWidget(table,1);selected={}
        def load(*_):
            order=self.orders.get(record['day'],record['store'],rev.currentIndex()+1);selected['order']=order;r=order['payload']['result'];note.setText(f"确认 {order['saved']} · 签发 ¥{r['summary']['amount']:,.2f} · {r['summary']['positive']} 款 · 只读；后续确认/下单/收货未接入")
            rows=r['rows'];table.setColumnCount(8);table.setHorizontalHeaderLabels(['商品编码','商品','推荐类型','原模型建议','候选数','签发数','签发金额','原因']);table.setRowCount(len(rows))
            for i,d in enumerate(rows):
                for j,val in enumerate([d['E'],d.get('D'),new_products.category(d),d.get('AS'),d.get('_candidate_qty'),order_quantity(d),order_amount(d),explanation(d)]):table.setItem(i,j,QTableWidgetItem(fmt(val)))
                table.setRowHidden(i,search.text().lower() not in (d['E']+' '+str(d.get('D',''))).lower())
            table.resizeColumnsToContents()
        rev.currentIndexChanged.connect(load);search.textChanged.connect(load)
        def audit():
            x=QDialog(dialog);x.setWindowTitle('完整审计快照（内部）');x.resize(900,650);layout=QVBoxLayout(x);text=QTextBrowser();text.setPlainText(wr.dumps(selected['order']));layout.addWidget(text);x.exec()
        row=QHBoxLayout();row.addWidget(button('导出此修订确认单',lambda:self.export_order(selected['order'])));row.addWidget(button('完整来源与参数审计',audit));row.addWidget(button('此单操作日志',lambda:self.show_events(selected['order']['oid'])));row.addWidget(button('关闭',dialog.accept));v.addLayout(row);rev.setCurrentIndex(record['revision']-1);load();dialog.exec()

    def show_events(self,oid=None):
        records=self.orders.events(oid);dialog=QDialog(self);dialog.setWindowTitle('归档操作日志明细');dialog.resize(1000,650);v=QVBoxLayout(dialog);search=QLineEdit();search.setPlaceholderText('查询时间、门店、单号、动作或文件名');v.addWidget(search);text=QTextBrowser();v.addWidget(text,1)
        def filtered():return [r for r in records if search.text().lower() in wr.dumps(r).lower()]
        def refresh(*_):text.setPlainText(json.dumps(filtered(),ensure_ascii=False,indent=2))
        def export():
            path,_=QFileDialog.getSaveFileName(dialog,'导出当前查询日志','归档日志.json','JSON (*.json)')
            if not path:return
            try:
                with open(path,'x',encoding='utf-8') as f:json.dump(filtered(),f,ensure_ascii=False,indent=2)
            except Exception as exc:QMessageBox.warning(dialog,'未导出',str(exc))
        row=QHBoxLayout();row.addWidget(button('导出当前查询',export));row.addWidget(button('关闭',dialog.accept));v.addLayout(row);search.textChanged.connect(refresh);refresh();dialog.exec()
