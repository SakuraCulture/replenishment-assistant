"""Lazy independent analysis page; bounded tables, no business-model dependency."""
from datetime import date,timedelta
from PySide6.QtCore import Qt,QDate
from PySide6.QtWidgets import (QWidget,QVBoxLayout,QHBoxLayout,QLabel,QTableWidget,QTableWidgetItem,
    QComboBox,QDateEdit,QTabWidget,QCheckBox,QDialog,QDialogButtonBox,QListWidget,QListWidgetItem,
    QMessageBox,QFileDialog,QLineEdit,QInputDialog)
from ui_controls import button,label
from stockout_common import VIEWS,today
from stockout_settings import route_label
from stockout_metrics import cut_columns,view_label

DIMENSIONS=[('门店','store'),('供货来源','source'),('必采属性','mandatory'),('供货仓','warehouse'),('商品类目','category'),('仓库上游供货商','supplier'),('仓商品状态','product_status'),('翱象补货状态','replen_status'),('在途情况','inbound_status'),('标签依据','tag_status'),('商品','sku')]
STATES={'PREPARED':'待开始','RUNNING':'进行中','PAUSED':'已安全暂停','REVIEW':'需核对后继续','COMPLETE':'已发布','PARTIAL':'已发布部分范围','ENDED':'本地已结束'}


def display(value,key=''):
    if key=='rate':return '—' if value is None else f'{value:.2%}'
    if key=='mandatory':return {1:'必采',0:'非必采',None:'属性待核'}[value]
    if key=='source':return {'center':'中心仓','local':'地采','pending':'归属待核'}.get(value,value)
    return '待核' if value is None else str(value)


def render(table,headers,rows):
    table.clear();table.setColumnCount(len(headers));table.setHorizontalHeaderLabels(headers);table.setRowCount(len(rows))
    table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    for i,row in enumerate(rows):
        for j,value in enumerate(row):
            item=QTableWidgetItem(str(value));item.setToolTip(str(value));table.setItem(i,j,item)
    table.resizeColumnsToContents()
    for column in range(len(headers)):
        table.setColumnWidth(column,min(320,max(90,table.columnWidth(column))))


class Page(QWidget):
    def __init__(self,host,entry):
        super().__init__(host);self.host,self.entry=host,entry
        self.settings=None;self.filters={};self.offset=0;self.cut_offset=0;self.cut_rows=[];self.last_error='';self.query_args=None
        self.pages=[];self.cut_pages=[];self.next_page=None;self.next_cut=None
        self.identity_names={}
        layout=QVBoxLayout(self)
        layout.addWidget(label('门店缺货率分析','title'))
        layout.addWidget(label('有月销商品中，门店可用库存为0的比例；资料独立留存，进入页面不自动采集。','muted'))
        self.basis=label('正在读取分析已采用版本…');layout.addWidget(self.basis)
        self.basis.setWordWrap(True)
        actions=QHBoxLayout()
        self.adopt=button('选门店 / 采用补货侧设置',self.choose_stores)
        self.supply=button('查看已采用供货设置',self.show_supply)
        self.collect=button('采集今日分析资料',self.start_collect,True)
        self.stop=button('安全暂停本次',lambda:entry.pause())
        self.background=QCheckBox('离开后本次继续采集');self.background.toggled.connect(self.background_changed)
        self.schedule=button('每日采集计划',self.configure_schedule)
        for widget in [self.adopt,self.supply,self.collect,self.stop,self.background,self.schedule]:actions.addWidget(widget)
        actions.addStretch();layout.addLayout(actions)
        filters=QHBoxLayout()
        self.start=QDateEdit();self.end=QDateEdit()
        for field in (self.start,self.end):field.setCalendarPopup(True);field.setDisplayFormat('yyyy-MM-dd');field.setDate(QDate.fromString(today(),'yyyy-MM-dd'))
        self.start.setDate(self.start.date().addDays(-29))
        self.view=QComboBox()
        for key,name in VIEWS.items():self.view.addItem(name,key)
        filters.addWidget(QLabel('观测日期'));filters.addWidget(self.start);filters.addWidget(QLabel('至'));filters.addWidget(self.end)
        filters.addWidget(self.view);filters.addWidget(button('查询已留存结果',self.refresh));filters.addWidget(button('清空切面筛选',self.clear_filters))
        self.export_btn=button('导出本视角Excel',self.export)
        filters.addWidget(self.export_btn);layout.addLayout(filters)
        exclusions=QHBoxLayout()
        self.exclude_stopped=QCheckBox('剔除禁采（含翱象停采）');self.exclude_stopped.setChecked(True)
        self.exclude_disabled=QCheckBox('剔除供货仓禁用商品');self.exclude_disabled.setChecked(True)
        exclusions.addWidget(self.exclude_stopped);exclusions.addWidget(self.exclude_disabled)
        exclusions.addWidget(label('可与淘汰品、考核品组合；分子分母同步剔除，未知状态保留。','muted'));exclusions.addStretch()
        layout.addLayout(exclusions)
        self.filter_note=label('当前筛选：全部已选门店、全部商品维度');layout.addWidget(self.filter_note)
        self.total=label('尚未取得本范围的完整分析结果','section');layout.addWidget(self.total)
        self.coverage=label('未采日期保持空白；需要连续日快照时请单独启用每日采集。','muted');layout.addWidget(self.coverage)
        self.space=label('','muted');layout.addWidget(self.space)
        tabs=QTabWidget();layout.addWidget(tabs,1)
        overview=QWidget();ov=QVBoxLayout(overview)
        ov.addWidget(label('每日观测趋势 · 逐日显示分子、分母与覆盖；缺采日期不补0。'))
        self.trend=QTableWidget();ov.addWidget(self.trend);tabs.addTab(overview,'总览与趋势')
        cut=QWidget();cv=QVBoxLayout(cut);cr=QHBoxLayout()
        self.dimension=QComboBox();self.second=QComboBox();self.second.addItem('不交叉',None)
        for name,key in DIMENSIONS:self.dimension.addItem(name,key);self.second.addItem(name,key)
        cr.addWidget(QLabel('按'));cr.addWidget(self.dimension);cr.addWidget(QLabel('交叉'));cr.addWidget(self.second)
        cr.addWidget(button('查看此切面',self.refresh));cr.addWidget(button('按选中分组筛选',self.select_groups));cr.addWidget(label('同维度多选为或，跨维度为且；双击可直接筛选。'))
        cv.addLayout(cr);self.cuts=QTableWidget();self.cuts.cellDoubleClicked.connect(self.select_cut);cv.addWidget(self.cuts)
        self.cuts.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        cp=QHBoxLayout();cp.addWidget(button('上一页',lambda:self.cut_turn(-1)));cp.addWidget(button('下一页',lambda:self.cut_turn(1)))
        self.cut_note=label('');cp.addWidget(self.cut_note);cv.addLayout(cp);tabs.addTab(cut,'切面分析')
        detail=QWidget();dv=QVBoxLayout(detail);dr=QHBoxLayout()
        self.only_zero=QCheckBox('明细只看缺货（不改变上方分母）');self.excluded=QCheckBox('查看本视角已剔除商品')
        self.sku=QLineEdit();self.sku.setPlaceholderText('精确商品编码，可留空')
        dr.addWidget(self.only_zero);dr.addWidget(self.excluded);dr.addWidget(self.sku);dr.addWidget(button('查询商品',self.refresh))
        dv.addLayout(dr);self.details=QTableWidget();self.details.cellDoubleClicked.connect(self.explain);self.detail_rows=[];dv.addWidget(self.details)
        paging=QHBoxLayout();paging.addWidget(button('上一页',lambda:self.turn(-1)));paging.addWidget(button('下一页',lambda:self.turn(1)))
        self.page_note=label('');paging.addWidget(self.page_note);dv.addLayout(paging);tabs.addTab(detail,'商品与待核')
        tasks=QWidget();tv=QVBoxLayout(tasks);tr=QHBoxLayout()
        self.runs=QComboBox();tr.addWidget(self.runs,1)
        self.resume=button('继续原任务 / 补失败项',self.resume_run);tr.addWidget(self.resume)
        self.end_run_btn=button('结束本地准备',self.end_run);tr.addWidget(self.end_run_btn)
        tr.addWidget(button('刷新记录',self.refresh_metadata));tv.addLayout(tr)
        self.result_links=QCheckBox('显示已删日志的日报依据（可重算，已删日志不恢复）')
        self.result_links.toggled.connect(self.refresh_metadata);tv.addWidget(self.result_links)
        self.run_table=QTableWidget();tv.addWidget(self.run_table)
        self.runs.currentIndexChanged.connect(self.load_run)
        tv.addWidget(label('已有任务先继续原任务；平台受理未知时只核对原结果，不重新导出。停止分析不会撤销平台导出。','muted'))
        operations=QHBoxLayout()
        self.rerun_btn=button('重新采集今日资料',lambda:self.run_action('rerun'))
        self.recalculate_btn=button('用留存原件重算',lambda:self.run_action('recalculate'))
        self.delete_log_btn=button('删除所选作业日志',lambda:self.run_action('delete_log'))
        for widget in (self.rerun_btn,self.recalculate_btn,self.delete_log_btn):operations.addWidget(widget)
        tv.addLayout(operations)
        note=label('重采：新建今日平台采集。重算：仅处理当前已发布作业，使用当时原件，不联网。\n删除作业日志保留分析结果和必要关联；删除分析数据请使用下面的日期范围入口。','muted');note.setWordWrap(True);tv.addWidget(note)
        self.delete_data_btn=button('删除分析数据…（按上方日期范围预览）',self.cleanup);tv.addWidget(self.delete_data_btn)
        self.retry_cleanup=button('重试未完成的分析原件清理',self.resume_cleanup);tv.addWidget(self.retry_cleanup)
        tabs.addTab(tasks,'采集与快照')
        tabs.currentChanged.connect(lambda index:self.load_run() if index==3 else None)
        for widget,signal in [(self.start,'dateChanged'),(self.end,'dateChanged'),(self.view,'currentIndexChanged'),
                              (self.dimension,'currentIndexChanged'),(self.second,'currentIndexChanged'),
                              (self.only_zero,'toggled'),(self.excluded,'toggled'),(self.sku,'textChanged'),
                              (self.exclude_stopped,'toggled'),(self.exclude_disabled,'toggled')]:
            getattr(widget,signal).connect(self.reset_pages)
        self.note=label(entry.message);self.note.setWordWrap(True);layout.addWidget(self.note)
        self.controls()

    def background_changed(self,checked):
        if self.entry.worker and self.entry.worker.operation in {'collect','rerun','recalculate'}:self.entry.background=checked

    def controls(self):
        active=self.entry.worker is not None
        for widget in (self.adopt,self.collect,self.resume,self.end_run_btn,self.rerun_btn,self.recalculate_btn,self.delete_log_btn,self.delete_data_btn):widget.setEnabled(not active)
        self.collect.setEnabled(not active and self.settings is not None)
        self.stop.setEnabled(active or self.entry.queued is not None)

    def refresh_metadata(self):self.entry.start('open',{'include_result_links':self.result_links.isChecked()},callback=self.metadata)

    def metadata(self,value):
        self.settings=value['settings']
        usage=value['space'];self.pending_cleanups=value['cleanups'];self.retry_cleanup.setEnabled(bool(self.pending_cleanups))
        self.space.setText(f"分析库 {usage['database']/1024**2:.1f} MiB · 自有原件 {usage['originals']/1024**2:.1f} MiB · 磁盘剩余 {usage['free']/1024**3:.1f} GiB（按记账与文件大小，不扫描全部原件）")
        if self.settings:
            p=self.settings.get('profile')
            stores=self.settings['stores'];routes=self.settings.get('routes',{});configured=sum(s['code'] in routes for s in stores)
            warning=f'；{len(stores)-configured}家未取得供货关系，请查看已采用设置并重新采用' if configured<len(stores) else ''
            self.basis.setText(f"分析采用：{len(stores)}家门店 · 供货关系 {configured}/{len(stores)}家 · 设置版本 {self.settings['version'][:12]} · 必卖清单 {p.get('revision','待核') if p else '待核'}{warning}；更新仅影响分析新任务")
        else:self.basis.setText('尚未采用分析设置，请先选择门店；不会修改补货侧设置。')
        previous=self.runs.currentData();self.runs.blockSignals(True);self.runs.clear()
        for run in value['runs']:
            action={'recalculate':'原件重算','rerun':'重新采集'}.get(run['trigger'],'采集')
            current=' · 当前日报 '+run['current_dates'] if run.get('current_dates') else ''
            self.runs.addItem(f"{run['created'][5:16].replace('T',' ')} · {action} · {STATES.get(run['state'],run['state'])}{current} · {run['message']}",run['id'])
        index=self.runs.findData(previous);self.runs.setCurrentIndex(max(index,0));self.runs.blockSignals(False)
        self.controls()
        self.run_table.setRowCount(0)
        self.refresh()

    def choose_stores(self):self.entry.start('choices',callback=self.stores_ready)

    def show_supply(self):
        if not self.settings:
            QMessageBox.information(self,'尚未采用设置','请先选择门店并采用补货侧设置。');return
        dialog=QDialog(self);dialog.setWindowTitle('分析已采用的门店供货设置');dialog.resize(1050,520);layout=QVBoxLayout(dialog)
        note=label('以下为分析独立保存的设置。参数页保存主从仓后，请重新“采用补货侧设置”；旧日报保持当时依据。');note.setWordWrap(True);layout.addWidget(note)
        routes=self.settings.get('routes',{});issues=self.settings.get('route_issues',{})
        rows=[]
        for store in self.settings['stores']:
            route=routes.get(store['code'],{})
            rows.append([store['code'],store['name'],'双仓' if route.get('secondary') else '单仓' if route else '未配置',
                         route.get('primary','未取得'),route.get('secondary') or '无从仓',
                         ('按主从顺序判断，商品资料取最终归属仓' if route.get('secondary') else '仅按本仓资料；本仓无该商品按本次地采') if route else issues.get(store['code'],'原分析未取得供货设置，请重新采用')])
        table=QTableWidget();render(table,['门店编码','门店名称','供货方式','主仓','从仓','资料取用 / 待处理原因'],rows);layout.addWidget(table)
        buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Close);buttons.button(QDialogButtonBox.StandardButton.Close).setText('关闭');buttons.rejected.connect(dialog.reject);layout.addWidget(buttons);dialog.exec()

    def stores_ready(self,value):
        if not value['configured'] or not value['stores']:
            QMessageBox.information(self,'连接待配置','请先在原翱象连接配置中核验并保存门店，本分析复用已有连接。');return
        dialog=QDialog(self);dialog.setWindowTitle('分析所选门店与资料采用');dialog.resize(620,600);layout=QVBoxLayout(dialog)
        note=label('复制参数设置中已保存的主从仓和必卖清单；两边之后互不自动更新。未配置供货关系的门店仍可统计全量，但仓、供应商及标签会待核。');note.setWordWrap(True);layout.addWidget(note)
        search=QLineEdit();search.setPlaceholderText('搜索门店名称 / 编码');layout.addWidget(search)
        listing=QListWidget();layout.addWidget(listing)
        selected={s['code'] for s in (self.settings or {}).get('stores',[])}
        for store in value['stores']:
            text=store['name']+' · '+store['code']+' · '+route_label(value.get('routes',{}).get(store['code']))
            item=QListWidgetItem(text);item.setToolTip(text+'\n'+value.get('route_issues',{}).get(store['code'],''));item.setData(Qt.ItemDataRole.UserRole,store)
            item.setFlags(item.flags()|Qt.ItemFlag.ItemIsUserCheckable);item.setCheckState(Qt.CheckState.Checked if store['code'] in selected else Qt.CheckState.Unchecked);listing.addItem(item)
        search.textChanged.connect(lambda text:[listing.item(i).setHidden(text.casefold() not in listing.item(i).text().casefold()) for i in range(listing.count())])
        row=QHBoxLayout();row.addWidget(button('勾选当前筛选',lambda:[listing.item(i).setCheckState(Qt.CheckState.Checked) for i in range(listing.count()) if not listing.item(i).isHidden()]))
        row.addWidget(button('清空勾选',lambda:[listing.item(i).setCheckState(Qt.CheckState.Unchecked) for i in range(listing.count())]));layout.addLayout(row)
        builtin=QCheckBox('原业务未保存必卖清单时，明确采用应用内置清单到分析');layout.addWidget(builtin)
        buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Ok|QDialogButtonBox.StandardButton.Cancel);buttons.accepted.connect(dialog.accept);buttons.rejected.connect(dialog.reject);layout.addWidget(buttons)
        if dialog.exec()!=QDialog.DialogCode.Accepted:return
        stores=[listing.item(i).data(Qt.ItemDataRole.UserRole) for i in range(listing.count()) if listing.item(i).checkState()==Qt.CheckState.Checked]
        if not stores:QMessageBox.information(self,'尚未选择','请选择至少一家门店');return
        self.entry.start('adopt',{'stores':stores,'use_builtin':builtin.isChecked()},lambda _:self.refresh_metadata())

    def options(self):
        return {'start':self.start.date().toString('yyyy-MM-dd'),'end':self.end.date().toString('yyyy-MM-dd'),
                'view':self.view.currentData(),'filters':self.filters.copy(),
                'exclude_stopped':self.exclude_stopped.isChecked(),'exclude_disabled':self.exclude_disabled.isChecked()}

    def dimensions(self):return list(dict.fromkeys(k for k in (self.dimension.currentData(),self.second.currentData()) if k))

    def refresh(self):
        self.query_args={'query':self.options(),'dimensions':self.dimensions(),'offset':self.offset,'cut_offset':self.cut_offset,
                         'only_zero':self.only_zero.isChecked(),'excluded':self.excluded.isChecked(),'sku':self.sku.text().strip()}
        self.total.setText('当前条件正在查询，旧结果已隐藏…');self.details.setRowCount(0);self.cuts.setRowCount(0);self.trend.setRowCount(0);self.detail_rows=[];self.cut_rows=[]
        self.entry.start('query',self.query_args,self.result)

    def result(self,value):
        row=value['total'];view=view_label(value['query'])
        prefix='所选范围门店缺货率' if len(value['coverage'])<=1 else '期间门店缺货率（店品日加权）'
        self.total.setText(f"{prefix}：{display(row['rate'],'rate')}　缺货 {row['zero']} / 有销 {row['base']}　· {view}　· {row['status']}；标签待核 {row['unknown_tags']}，剔除有销 {row['removed_base']}")
        coverage=value['coverage'];complete=sum(r['completed'] for r in coverage);selected=sum(r['selected'] for r in coverage)
        self.coverage.setText(f'实际留存 {len(coverage)} 个日期，已完成 {complete}/{selected} 个门店日；仅此覆盖范围，未采日期不补0。')
        render(self.trend,['观测日期','缺货数','有销基数','门店缺货率','指标待核','标签待核','完成/所选门店'],
               [[r['day'],r['total']['zero'],r['total']['base'],display(r['total']['rate'],'rate'),r['total']['bad'],r['total']['unknown_tags'],
                 '、'.join(f"{c['completed']}/{c['selected']}" for c in r['coverage'])] for r in value['trend']])
        cuts=value['cuts'];self.cut_rows=cuts['items'];self.cut_dims=cuts['dimensions']
        self.next_cut=cuts['next_offset'];self.next_page=value['details']['next_offset']
        columns=cut_columns(self.cut_dims)
        self.identity_names={key:name for key,name in self.identity_names.items() if key[1] in self.filters.get(key[0],[])}
        for r in self.cut_rows:
            for dimension in ('store','sku'):
                if dimension in self.cut_dims:self.identity_names[dimension,r[dimension]]=r[dimension+'_name']
        render(self.cuts,[*[title for _,title in columns],'有销基数','缺货数','门店缺货率','标签待核','指标待核'],
               [[*[display(r[key],key) for key,_ in columns],r['base'],r['zero'],display(r['rate'],'rate'),r['unknown_tags'],r['bad']] for r in self.cut_rows])
        self.cut_note.setText(f"第 {self.cut_offset+1} 起，当前 {len(self.cut_rows)} 组 / 共 {cuts['total']} 组")
        rows=value['details']['items'];self.detail_rows=rows
        render(self.details,['日期','门店编码','门店名称','商品编码','商品名称','月销','可用库存','供货来源','仓','必采','类目','上游供货商','虚拟分类','补货状态','仓商品状态','说明'],
               [[r['day'],r['store'],r['store_name'],r['sku'],r['name'],r['month_raw'],r['stock_raw'],display(r['source'],'source'),r['warehouse'],
                 display(r['mandatory'],'mandatory'),r['category'],r['supplier'],display(r['tags']),r['replen_status'],r['product_status'],r['error']] for r in rows])
        self.page_note.setText(f"第 {self.offset+1} 起，当前 {len(rows)} 条 / 共 {value['details']['total']} 条；每页最多200条")

    def explain(self,index,column):
        if index>=len(self.detail_rows):return
        r=self.detail_rows[index]
        text=f"门店：{r['store_name']}（{r['store']}）\n商品：{r['name']}（{r['sku']}）\n观测时间：{r['observed']}\n原件行号：{r['rownum']}\n日报修订：{r['revision']}\n原件哈希：{r['source_sha']}\n\n月销：{r['month_raw']}；可用库存：{r['stock_raw']}；在途：{r['inbound_raw']}\n仓商品状态：{r['product_status']}；翱象补货状态：{r['replen_status']}\n来源：{display(r['source'],'source')}；仓：{r['warehouse']}\n虚拟分类：{display(r['tags'])}\n\n{r['error']}"
        QMessageBox.information(self,'商品口径与原件依据',text)

    def select_cut(self,index,column):
        if index>=len(self.cut_rows):return
        self.apply_groups([index])

    def select_groups(self):
        self.apply_groups(sorted({index.row() for index in self.cuts.selectedIndexes()}))

    def apply_groups(self,indices):
        if not indices:return
        for dimension in self.cut_dims:self.filters[dimension]=list(dict.fromkeys(self.cut_rows[i][dimension] for i in indices))
        self.reset_pages()
        names=dict((key,name) for name,key in DIMENSIONS)
        def title(key,value):
            name=self.identity_names.get((key,value))
            return name+'（'+str(value)+'）' if name else display(value,key)
        self.filter_note.setText('当前筛选：'+'；'.join(names.get(k,k)+'='+','.join(title(k,v) for v in values) for k,values in self.filters.items()))
        self.refresh()

    def clear_filters(self):
        self.filters={};self.reset_pages();self.filter_note.setText('当前筛选：全部已选门店、全部商品维度');self.refresh()

    def reset_pages(self,*_):
        self.offset=self.cut_offset=0;self.pages=[];self.cut_pages=[];self.next_page=self.next_cut=None

    def turn(self,step):
        if step>0:
            if self.next_page is None:return
            self.pages.append(self.offset);self.offset=self.next_page
        else:self.offset=self.pages.pop() if self.pages else 0
        self.refresh()

    def cut_turn(self,step):
        if step>0:
            if self.next_cut is None:return
            self.cut_pages.append(self.cut_offset);self.cut_offset=self.next_cut
        else:self.cut_offset=self.cut_pages.pop() if self.cut_pages else 0
        self.refresh()

    def start_collect(self):
        self.entry.start('collect',{},lambda _:self.refresh_metadata(),background=self.background.isChecked())

    def load_run(self):
        rid=self.runs.currentData()
        if rid:self.entry.start('run',{'run':rid},self.run_ready)

    def run_ready(self,value):
        render(self.run_table,['门店名称','门店编码','状态','说明'],[[s['name'],s['store'],{'READY':'已留存','FAILED':'未完成','PENDING':'待处理','CLEANED':'分析数据已删除'}.get(s['state'],s['state']),s['error']] for s in value['stores']])
        self.entry.status(STATES.get(value['state'],value['state'])+'：'+value['message'])

    def resume_run(self):
        rid=self.runs.currentData()
        if not rid:return
        def ready(run):
            if run['external'].get('recalculate'):
                if run['state'] in {'COMPLETE','PARTIAL','ENDED'}:
                    QMessageBox.information(self,'重算已结束','可点击“用留存原件重算”新建修订，或“重新采集今日资料”。');return
                self.entry.start('recalculate',{'resume':rid},lambda _:self.refresh_metadata(),background=self.background.isChecked());return
            args={'source_run':rid} if run['state'] in {'PARTIAL','COMPLETE'} else {'resume':rid}
            if run['state']=='ENDED':QMessageBox.information(self,'任务已结束','请采集今日数据创建新任务，原平台防重保护仍然有效。');return
            self.entry.start('collect',args,lambda _:self.refresh_metadata(),background=self.background.isChecked())
        self.entry.start('run',{'run':rid},ready)

    def run_action(self,action):
        rid=self.runs.currentData()
        if not rid:return
        mode='current'
        if action=='recalculate':
            choice,ok=QInputDialog.getItem(self,'选择重算依据','保留原门店范围及观测日期，选择供货规则和必卖清单：',
                ['当前分析已采用设置（先采用参数页保存的设置）','原作业当时设置'],0,False)
            if not ok:return
            mode='current' if choice.startswith('当前') else 'original'
        def ready(plan):
            names='、'.join(s['name']+'（'+s['code']+'）' for s in plan['stores'])
            titles={'rerun':'重新采集今日资料','recalculate':'用留存原件重算','delete_log':'删除所选作业日志'}
            details='\n日期：'+plan['day'] if action=='rerun' else ''
            if action=='recalculate':
                details='\n观测日期：'+ '、'.join(sorted(plan['publications']))+f"\n重算 {len(plan['snapshots'])} 家；原作业缺少完整快照 {plan['missing']} 家，仍不补0。\n设置版本：{plan['basis'][:12]}"
            text=f"门店范围：{names}{details}\n\n{plan['scope']}。"
            if QMessageBox.question(self,titles[action],text,QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.Cancel,QMessageBox.StandardButton.Cancel)!=QMessageBox.StandardButton.Yes:return
            def complete(value):
                if action=='delete_log':QMessageBox.information(self,'作业日志已删除',value['message'])
                self.refresh_metadata()
            self.entry.start('delete_run_log' if action=='delete_log' else action,{'plan':plan},complete,
                             background=self.background.isChecked() if action!='delete_log' else False)
        self.entry.start('run_action_preview',{'run':rid,'action':action,'mode':mode},ready)

    def end_run(self):
        rid=self.runs.currentData()
        if rid:self.entry.start('end',{'run':rid},lambda _:self.refresh_metadata())

    def configure_schedule(self):
        from stockout_trigger import load
        if not self.settings:QMessageBox.information(self,'尚未采用设置','请先选择分析门店并采用设置。');return
        current=load(self.host.repository.folder)
        dialog=QDialog(self);dialog.setWindowTitle('分析每日采集计划');layout=QVBoxLayout(dialog)
        enabled=QCheckBox('启用每日后台采集');enabled.setChecked(current['enabled']);layout.addWidget(enabled)
        at=QLineEdit(current.get('at','08:00'));layout.addWidget(label('北京时间 HH:mm'));layout.addWidget(at)
        note=label('启用后，未打开分析页面也会到时取数，并占用浏览器、内存和硬盘；任务结束后释放。\n软件关闭、休眠、未登录或原业务繁忙时可能缺采；缺采日期不能用今天库存补回。\n停用未来计划不会撤销已经受理的导出，可单独安全暂停本次任务。');note.setWordWrap(True);layout.addWidget(note)
        buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Ok|QDialogButtonBox.StandardButton.Cancel);buttons.accepted.connect(dialog.accept);buttons.rejected.connect(dialog.reject);layout.addWidget(buttons)
        if dialog.exec()==QDialog.DialogCode.Accepted:
            try:self.entry.set_schedule(enabled.isChecked(),at.text().strip())
            except ValueError as exc:QMessageBox.information(self,'计划未保存',str(exc))

    def export(self):
        destination,_=QFileDialog.getSaveFileName(self,'导出独立门店缺货率报表','门店缺货率_'+today()+'.xlsx','Excel (*.xlsx)')
        if not destination:return
        details=QMessageBox.question(self,'商品明细','是否同时导出商品明细？大范围明细将逐行写入，可能耗时。',QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No,QMessageBox.StandardButton.No)==QMessageBox.StandardButton.Yes
        self.entry.start('export',{'query':self.options(),'dimensions':self.dimensions(),'destination':destination,'include_detail':details},lambda path:self.entry.status('已导出：'+path))

    def cleanup(self):
        options=self.options()
        def ready(plan):
            text=f"日期：{plan['start']} 至 {plan['end']}\n将清理 {plan['snapshots']} 份门店快照、{plan['rows']} 条商品记录、{plan['revisions']} 份日报修订。\n{plan['scope']}。\n清理后这些日期不能继续下钻或复算；不按当前类目/供货商筛选缩小范围。"
            if QMessageBox.warning(self,'确认清理分析历史',text,QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.Cancel,QMessageBox.StandardButton.Cancel)!=QMessageBox.StandardButton.Yes:return
            def complete(value):
                QMessageBox.information(self,'分析清理结果',value['message']+('\n待重试清理编号：'+value['id'] if value['pending'] else ''))
                self.refresh_metadata()
            self.entry.start('cleanup',{'plan':plan},complete)
        self.entry.start('cleanup_preview',{'start':options['start'],'end':options['end']},ready)

    def resume_cleanup(self):
        if getattr(self,'pending_cleanups',None):
            self.entry.start('cleanup_retry',{'id':self.pending_cleanups[-1]['id']},lambda _:self.refresh_metadata())
