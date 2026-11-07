from __future__ import annotations
import export_locations
from display_fields import COLUMNS
"""Shared goods table, filters, detail and result export interaction.

Host creates pages/status first, owns loaded/result/dirty and selected_store(),
and supplies job()/update_controls(). make_results() explicitly installs the view.
"""
import sys,os,json,threading,traceback,html,logging
from pathlib import Path
from datetime import datetime
from PySide6.QtCore import Qt,Signal,QThread,QAbstractTableModel,QSortFilterProxyModel,QModelIndex,QTimer,QSignalBlocker,QUrl,QStandardPaths
from PySide6.QtGui import QColor,QFont,QDesktopServices,QIcon,QPalette
from PySide6.QtWidgets import (QApplication,QMainWindow,QWidget,QVBoxLayout,QHBoxLayout,QLabel,QPushButton,QFrame,
    QFileDialog,QComboBox,QCheckBox,QDoubleSpinBox,QTableWidget,QTableWidgetItem,QHeaderView,QTableView,QLineEdit,
    QStackedWidget,QSplitter,QTextBrowser,QMessageBox,QDialog,QProgressBar,QAbstractItemView,QScrollArea,QGridLayout)
import result_semantics as semantics
import review_amounts
from ui_review_amounts import ReviewAmounts
from ui_review_filters import ReviewFilters
import new_product_focus
import review_filters
import engine
import priority_list
from version_info import VERSION


from ui_controls import label,button

from ui_goods_fast import GoodsModel,FilterModel


class SummaryTask(QThread):
    result=Signal(object)
    def __init__(self,rows,parent):super().__init__(parent);self.rows=rows
    def run(self):self.result.emit(semantics.summarize(self.rows));self.rows=None


class ResultsView:
    def make_results(self):
        page=QWidget();v=QVBoxLayout(page);v.setContentsMargins(0,0,0,0);v.setSpacing(12)
        self.result_caption=label('尚未计算','section');v.addWidget(self.result_caption)
        self.admission_btn=button('查看本店准入问题 / 就地维护',self.review_admission);v.addWidget(self.admission_btn)
        metrics=QHBoxLayout();self.metric_values=[]
        for title in ['执行补货金额（元）','执行补货商品','一箱例外候选 / 候选金额','地采 / 已排除']:
            c=QFrame();c.setObjectName('card');cv=QVBoxLayout(c);cv.setContentsMargins(16,12,16,12);cv.addWidget(label(title,'muted'));x=label('—','metric');cv.addWidget(x);metrics.addWidget(c);self.metric_values.append(x)
        v.addLayout(metrics)
        self.detail_review_amounts=ReviewAmounts();v.addWidget(self.detail_review_amounts)
        self.budget_caption=label('','muted');v.addWidget(self.budget_caption)
        self.result_facets=ReviewFilters();v.addWidget(self.result_facets)
        tools=QHBoxLayout();self.mode=QComboBox();self.mode.setToolTip('快捷条件；仍与上方组合筛选同时生效');self.mode.addItems(['全部商品','建议补货','必卖商品','预算未满足','最低持有缺口','资料匹配','一箱例外','地采商品','待处理','重点关注']);tools.addWidget(self.mode)
        self.warehouse_filter=QComboBox();self.warehouse_filter.addItem('全部仓');tools.addWidget(self.warehouse_filter)
        self.mode.addItems(['人工改量商品','签发单新品'])
        self.category_filter=QComboBox();self.category_filter.addItem('全部分类');tools.addWidget(self.category_filter)
        self.search=QLineEdit();self.search.setPlaceholderText('搜索商品名称 / 编码 / 条码');self.search.setClearButtonEnabled(True);tools.addWidget(self.search,1)
        self.export_all=button('导出补货清单与明细',lambda:self.export(False),True);tools.addWidget(self.export_all)
        self.export_filtered=button('导出当前筛选',lambda:self.export(True));tools.addWidget(self.export_filtered);v.addLayout(tools)
        tools.addWidget(button('查看排除商品',self.show_excluded));tools.addWidget(button('重置筛选',self.reset_result_filters))
        self.filtered_caption=label('','muted');v.addWidget(self.filtered_caption)
        splitter=QSplitter();self.model=GoodsModel();self.proxy=FilterModel();self.proxy.setSourceModel(self.model)
        self.table=QTableView();self.table.setModel(self.proxy);self.table.setSortingEnabled(True);self.table.setAlternatingRowColors(True);self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows);self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection);self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers);self.table.verticalHeader().setDefaultSectionSize(38);self.table.verticalHeader().hide()
        for i,w in enumerate([300,180,105,85,110,70,85,70,70,90,90,90,110,340,110,100,190,90,90]):self.table.setColumnWidth(i,w)
        self.table.horizontalHeader().moveSection(19,2)
        self.table.sortByColumn(10,Qt.SortOrder.DescendingOrder)
        from table_readability import install
        install(self.table)
        splitter.addWidget(self.table)
        self.detail_panel=QWidget();dv=QVBoxLayout(self.detail_panel);dv.setContentsMargins(8,0,0,0);dr=QHBoxLayout();dr.addWidget(label('商品计算依据','section'));dr.addStretch();dr.addWidget(button('关闭',self.detail_panel.hide));dv.addLayout(dr)
        self.details=QTextBrowser();dv.addWidget(self.details);self.detail_panel.setMinimumWidth(340);splitter.addWidget(self.detail_panel);splitter.setStretchFactor(0,3);splitter.setStretchFactor(1,1);self.detail_panel.hide();v.addWidget(splitter,1)
        self.table.clicked.connect(self.show_detail)
        for x in [self.mode,self.warehouse_filter,self.category_filter]:x.currentTextChanged.connect(self.filter_changed)
        self.filter_timer=QTimer(self.search);self.filter_timer.setSingleShot(True);self.filter_timer.setInterval(275);self.filter_timer.timeout.connect(self.filter_changed)
        self.search.textChanged.connect(lambda _:self.filter_timer.start());self.search.returnPressed.connect(self.filter_changed)
        self.filter_generation=0;self.summary_task=None;self.summary_pending=None
        self.active_conditions=QGridLayout();v.insertLayout(4,self.active_conditions)
        self.column_choice=QComboBox();self.column_choice.addItems(['主要结果','预算与调整','仓归属与资料','全部字段']);tools.insertWidget(0,self.column_choice)
        self.column_choice.currentTextChanged.connect(self.apply_column_group);self.apply_column_group()
        self.result_facets.changed.connect(self.filter_changed)
        self.result_facets.fields['group'].currentIndexChanged.connect(lambda *_:self.mode.setCurrentText('全部商品'))
        QApplication.instance().aboutToQuit.connect(self.stop_filtered_summary)
        self.pages.addWidget(page)

    def review_admission(self):
        from admission_ui import open_editor
        if not hasattr(self,'work_repository'):return
        from business_ui import grid
        d=QDialog(self);d.setWindowTitle('本店准入问题');d.resize(950,620);v=QVBoxLayout(d)
        rows=[[r['E'],r.get('D'),r.get('AU'),r['_meta'].get('product_tags'),'；'.join(r['_meta'].get('admission',{}).get('reasons',[]))] for r in (self.result or {}).get('rows',[]) if r['_meta'].get('admission',{}).get('reasons')]
        v.addWidget(label(f'{len(rows)}款未安排；原计算建议保留。保存准入变更后需重算本店。','section'))
        table,model=grid(d,['商品编码','商品名称','仓库','标签原文','原因'],rows);v.addWidget(table,1)
        v.addWidget(button('维护本店资质 / 拒收',lambda:(d.accept(),open_editor(self,self,self.selected_store())),True));v.addWidget(button('关闭',d.reject));d.exec()

    def set_rows(self,rows,calculated):
        self.admission_btn.setVisible(any(r.get('_meta',{}).get('admission',{}).get('reasons') for r in rows))
        self.filter_timer.stop();sort_column=self.proxy.sortColumn();sort_order=self.proxy.sortOrder();self.proxy.sort(-1)
        blockers=[QSignalBlocker(w) for w in (self.search,self.warehouse_filter,self.category_filter,self.mode,self.result_facets)]
        self.result_facets.reset()
        self.model.set_rows(rows);self.apply_column_group();self.detail_panel.hide();self.search.clear()
        self.warehouse_filter.clear();self.warehouse_filter.addItems(['全部仓']+sorted({str(d.get('AU','')) for d in rows}))
        self.category_filter.clear();self.category_filter.addItems(['全部分类']+sorted({str(d.get('_category') or '未分类') for d in rows}))
        self.mode.setCurrentText('建议补货' if calculated else '全部商品');del blockers;self._pending_sort=(sort_column,sort_order);self.filter_changed()
        # Published calculation results already carry the exact full-row summary.
        # A filtered/different list still needs its own summary.
        s=self.result['summary'] if calculated and self.result and rows is self.result['rows'] else semantics.summarize(rows)
        self.metric_values[0].setText(f"{s['amount']:,.2f}" if calculated else '待计算')
        self.metric_values[1].setText(f"{s['positive']:,}" if calculated else '待计算')
        self.metric_values[2].setText(f"{s['exception']} / +{s['extra']:,.2f}" if calculated else '待计算')
        self.metric_values[3].setText(f"{s['local']:,} / {s['excluded']:,}")
        self.detail_review_amounts.set_summary((review_amounts.cached(self,self.result) if self.result and rows is self.result['rows'] else review_amounts.summarize(rows)) if calculated else None)
        self.detail_review_amounts.setToolTip('本店完整结果的金额；不随下方仓库、分类或搜索筛选变化。'+review_amounts.NOTE)
        name=self.loaded['stores'].get(self.selected_store(),'') if self.loaded else ''
        self.result_caption.setText(f'{name} · '+('计算完成' if calculated else '导入预览 · 尚未计算')+ (f" · {s['unpriced']}款补货未计价" if s['unpriced'] else ''))
        if calculated and self.result:
            from restock_policy import summary
            self.result_caption.setText(self.result_caption.text()+'\n'+summary(self.result.get('config',{})))
        if calculated:
            risk_count=sum(bool(semantics.inbound_risk(d)) for d in rows)
            if risk_count:self.result_caption.setText(self.result_caption.text()+f' · 在途衔接风险 {risk_count} 条（筛选查看，不自动补回）')
        b=self.result.get('budget',{}) if calculated and self.result else {}
        if b.get('enabled'):
            self.budget_caption.setText(f"预算 {b['limit']:,.2f} 元 · 已用 {b['spent']:,.2f} · 剩余 {b['remaining']:,.2f} · 必卖 {b['must_spent']:,.2f} / 普通 {b['other_spent']:,.2f} · 未满足 {b['deferred']} 款 · 最低持有缺口 {b['holding_gaps']} 款")
            over=b.get('overrun',{})
            if over.get('amount',0)>0:self.budget_caption.setText(self.budget_caption.text()+f"\n有限超支 {over['amount']:.2f} 元；商品 {over['sku']} 追加 {over['quantity']:g} 个库存单位；本店金额上限 {over['ceiling']:.2f} 元。")
        else:self.budget_caption.setText('总预算未启用；保持原执行建议。' if calculated else '预算分配在原计算和禁用/停采排除之后执行。')
        if calculated and self.result.get('must_list'):
            ml=self.result['must_list'];self.budget_caption.setText(self.budget_caption.text()+f"\n必卖清单 {ml['total']} 款，本店命中 {ml['matched']} 款，{ml['missing']} 款未出现在本店导出；不自动新增补货。")

    def apply_column_group(self,*_):
        group=self.column_choice.currentText()
        visible={'主要结果':{0,1,3,5,6,7,8,9,10,13,15},'预算与调整':{0,1,3,9,10,13,18,19,20,21,22,23,24},'仓归属与资料':{0,1,2,3,13,14,*range(25,34)}}.get(group,set(range(len(COLUMNS))))
        for col in range(len(COLUMNS)):self.table.setColumnHidden(col,col not in visible)
    def condition_buttons(self):
        while self.active_conditions.count():
            item=self.active_conditions.takeAt(0)
            if item.widget():item.widget().hide();item.widget().deleteLater()
        items=[label('当前条件：','muted')]
        controls=[self.mode,self.warehouse_filter,self.category_filter,*self.result_facets.fields.values()]
        for c in controls:
            if c.currentIndex()>0:items.append(button(c.currentText()+' ×',lambda checked=False,w=c:w.setCurrentIndex(0)))
        if self.search.text():
            b=button('搜索：'+self.search.text()[:30]+' ×',self.search.clear);b.setToolTip(self.search.text());items.append(b)
        items.append(button('清除全部条件',self.reset_result_filters))
        for i,w in enumerate(items):self.active_conditions.addWidget(w,i//4,i%4)
    def visible_rows(self):return [self.model.rows[self.proxy.mapToSource(self.proxy.index(i,0)).row()] for i in range(self.proxy.rowCount())]

    def filter_changed(self,*a):
        self.filter_timer.stop();self.filter_generation+=1;self.summary_pending=None;self.condition_buttons()
        self.proxy.beginFilterChange();self.proxy.mode=self.mode.currentText();self.proxy.warehouse=self.warehouse_filter.currentText() or '全部仓';self.proxy.category=self.category_filter.currentText() or '全部分类';self.proxy.search=self.search.text().strip().lower();self.proxy.facets=self.result_facets.values();self.proxy.focused=new_product_focus.members(getattr(self,'new_focus',None));self.proxy.focus_known=getattr(self,'new_focus',None) is not None;self.proxy.endFilterChange(QSortFilterProxyModel.Direction.Rows)
        if hasattr(self,'_pending_sort'):
            self.proxy.sort(*self._pending_sort);del self._pending_sort
        rows=self.visible_rows()
        self.result_facets.set_counts(review_filters.facet_counts((d for d in self.model.rows if self.proxy.accepts(d,True)),self.proxy.facets,self.proxy.focused,self.proxy.focus_known))
        if hasattr(self,'sections'):self.export_filtered.setText(f'导出筛选内部明细（{len(rows)}行）');self.export_all.setToolTip('当前门店完整内部报告；不采用页面的仓、分类或商品筛选')
        if len(rows)<500:self.set_filtered_summary(semantics.summarize(rows))
        else:
            self.filtered_caption.setText(f'当前筛选 {len(rows):,} 行 · 统计中…');self.summary_pending=(self.filter_generation,rows);self.start_filtered_summary()

    def reset_result_filters(self):
        locks=[QSignalBlocker(w) for w in (self.search,self.warehouse_filter,self.category_filter,self.mode,self.result_facets)]
        self.search.clear();self.warehouse_filter.setCurrentIndex(0);self.category_filter.setCurrentIndex(0);self.mode.setCurrentText('全部商品');self.result_facets.reset();del locks;self.filter_changed()

    def show_excluded(self):
        d=QDialog(self);d.setWindowTitle('排除商品 · 只读，不参与签发');d.resize(1050,650);v=QVBoxLayout(d)
        v.addWidget(label('仅核对禁用／停采原因；查看不会恢复补货资格，不改变工作清单和导出。','muted'))
        tools=QHBoxLayout();scope=QComboBox();scope.addItems(['已排除','商品资料禁用','补货建议停采']);search=QLineEdit();search.setPlaceholderText('商品名称 / 编码 / 条码');tools.addWidget(scope);tools.addWidget(search,1);v.addLayout(tools)
        model=GoodsModel();model.set_rows(self.model.rows);proxy=FilterModel();proxy.include_excluded=True;proxy.setSourceModel(model)
        table=QTableView();table.setModel(proxy);table.setSortingEnabled(True);table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers);table.setAlternatingRowColors(True);v.addWidget(table,1);count=label('');v.addWidget(count)
        def refresh(*_):
            proxy.mode=scope.currentText();proxy.search=search.text().strip();proxy.invalidateFilter();count.setText(f'排除商品 {proxy.rowCount()} 款；原结果不变。')
        scope.currentIndexChanged.connect(refresh);search.textChanged.connect(refresh);refresh();v.addWidget(button('关闭',d.accept));d.exec()

    def stop_filtered_summary(self):
        self.summary_pending=None
        if self.summary_task:self.summary_task.wait()

    def start_filtered_summary(self):
        if self.summary_task is not None or self.summary_pending is None:return
        generation,rows=self.summary_pending;self.summary_pending=None
        task=SummaryTask(rows,self.table);self.summary_task=task
        def apply(s):
            if generation==self.filter_generation:self.set_filtered_summary(s)
        def finished():
            self.summary_task=None;task.deleteLater();self.start_filtered_summary()
        task.result.connect(apply);task.finished.connect(finished);task.start()

    def set_filtered_summary(self,s):
        self.filtered_caption.setText(f"当前筛选 {s['rows']:,} 行  ·  执行金额 {s['amount']:,.2f} 元  ·  计算金额 {s['calculated_amount']:,.2f} 元  ·  已排除 {s['excluded']:,} 行  ·  数据异常 {s['issues']:,} 行")

    def show_detail(self,index):
        from business_presenter import stage_rows,field_row,parameter_rows
        d=self.model.rows[self.proxy.mapToSource(index).row()];result=self.result or {}
        def table(rows):
            return '<table width="100%">'+''.join('<tr>'+''.join('<td style="padding:5px">'+html.escape(str(x))+'</td>' for x in row)+'</tr>' for row in rows)+'</table>'
        content='<h2>'+html.escape(str(d.get('D','')))+'</h2><p>'+html.escape(str(d.get('AU','')))+' · '+html.escape(str(d.get('E','')))+'</p>'
        content+='<h3>最终安排</h3>'+table([['数量',semantics.order_quantity(d),d.get('G') or '库存单位'],['金额',semantics.order_amount(d),'元']])
        content+='<p>'+html.escape(semantics.explanation(d))+'</p><h3>数量形成过程</h3>'+table(stage_rows(d,final_label='最终安排量（待签发）'))
        if d['_meta'].get('product_tags_known'):
            content+='<h3>商品标签</h3><p>'+html.escape(str(d['_meta'].get('product_tags') or '未标记标签'))+'</p>'
        content+='<h3>销量与库存</h3>'+table([field_row(d,k) for k in ('H','I','J','K','L','AG','AH','BD')])
        content+='<p><a href="parameters">查看实际参数</a> · <a href="technical">查看完整技术依据</a></p>'
        self.details.setOpenLinks(False)
        try:self.details.anchorClicked.disconnect()
        except RuntimeError:pass
        def follow(url):
            if url.toString()=='technical':self.show_technical_detail(d)
            elif url.toString()=='parameters':self.details.setHtml('<h3>本商品实际参数</h3>'+table(parameter_rows(result,d))+'<p><a href="back">返回数量说明</a></p>')
            else:self.details.setHtml(content)
        self.details.anchorClicked.connect(follow);self.details.setHtml(content);self.detail_panel.show()
    def show_technical_detail(self,d):
        esc=lambda x:html.escape(semantics.fmt(x))
        def group(title,items):return '<h3>'+title+'</h3><table width="100%">'+''.join(f'<tr><td style="color:#60777D;padding:5px 0">{html.escape(k)}</td><td align="right">{esc(v)}</td></tr>' for k,v in items)+'</table>'
        content=f'<h2>{esc(d.get("D"))}</h2><p>{esc(d.get("E"))} · {d["_source"]}</p>'
        content+='<p><b>'+esc(semantics.order_status(d))+'</b> '+esc(semantics.exclusion_reason(d))+'</p>'
        content+='<p>优先清单：'+('必卖商品' if d.get('_must_sell') else '普通商品')+'</p>'
        risk=semantics.inbound_risk(d)
        if risk:content+='<h3>需人工复核</h3><p>'+esc(risk['text'])+'</p>'
        elif d.get('_inbound_cap'):content+='<p>已有在途：追加保底已受库存额度限制，不重新补回。原库存额度口径保持不变。</p>'
        route=semantics.routing(d)
        if route:
            content+=group('需求归属',[('主仓',route['primary']),('从仓',route['secondary'] or '无'),('需求归属仓',route['assigned']),('主仓判断',route['primary_state']),('主仓可用数',route['primary_stock']),('从仓判断',route['secondary_state']),('从仓可用数',route['secondary_stock'])])+'<p>'+esc(route['reason'])+'</p><p>此阶段只确定归属，不拆单、不按仓库存限量或预占；从仓无货仍保留需求。禁用和停采按执行规则处理。</p>'
        if not d.get('_preview'):content+=group('本次执行',[('执行补货',semantics.order_quantity(d)),('执行金额',semantics.order_amount(d))])+group('计算参考（未按状态排除）',[('计算建议',semantics.quantity(d)),('计算金额',semantics.amount_value(d)),('按计算建议持有天数',semantics.coverage(d))])
        if d.get('_budget_active'):
            content+=group('资金分配',[('预算前执行量',d.get('_candidate_qty')),('预算前金额',d.get('_candidate_amount')),('缺口数量',d.get('_budget_gap')),('最低持有缺口','有' if d.get('_holding_gap') else '无')])+'<p>'+esc(d.get('_budget_reason'))+'</p>'
        content+=group('商品现状',[('所属仓',d.get('AU')),('导出状态',d['_meta'].get('export_status')),('中心仓状态',d.get('S')),('月销 / 周销',f'{semantics.fmt(d.get("J"))} / {semantics.fmt(d.get("I"))}'),('可用 / 在途',f'{semantics.fmt(d.get("K"))} / {semantics.fmt(d.get("L"))}'),('箱规',d.get('BD',d.get('O'))),('基本售价',d.get('V'))])
        if d['_source']=='地采商品':content+='<h3>地采商品</h3><p>商品编码未匹配资料。原始销量和库存保留；缺少价格、出库箱规等计算条件，本版不生成补货数量和金额。</p>'
        elif not d.get('_preview'):
            rate=d['AN']*d['AM'] if semantics.numeric(d.get('AN')) and semantics.numeric(d.get('AM')) else None
            current=(max(d['K'],0)+max(d['L'],0))/rate if rate and semantics.numeric(d.get('K')) and semantics.numeric(d.get('L')) else None
            content+=group('需求预测与库存额度',[('预测日均',rate),('额度基准日均 BH',d.get('BH')),('增长调整日均 BN',d.get('BN')),('当前可售天数',current),('到货天数',d.get('AH')),('备货天数',d.get('AG')),('安全库存',d.get('AL')),('库存额度总量',d.get('库存额度目标量'))])
            content+='<p style="color:#60777D">库存额度受历史销量约束，未统一为预测日均。可售天数按预测日均折算；无正预测时不显示为0天。</p>'
            content+=group('从需求到计算建议',[('净需求',d.get('AQ')),('向上整箱',d.get('BJ')),('额度后',d.get('AR')),('资金前',d.get('BR')),('原模型建议',d.get('_base_qty')),('原模型金额',d.get('_base_amount')),('计算建议',semantics.quantity(d)),('计算金额',semantics.amount_value(d)),('按计算建议持有天数',semantics.coverage(d))])
            a=d.get('_audit',{});content+='<h3>一箱例外</h3><p>'+esc(a.get('reason','未执行'))+'</p>'
            if a.get('applied'):content+=group('放行依据',[('原拦截',a.get('blocked_by')),('新增金额',a.get('incremental_amount'))])
        content+='<h3>结果说明</h3><p>'+esc(semantics.explanation(d))+'</p><p>'+esc('；'.join(d['_meta']['notes']))+'</p>'
        self.details.setHtml(content+'<p><a href="back">返回数量说明</a></p>');self.detail_panel.show()

    def export(self,filtered):
        if not self.result or self.dirty:return
        if filtered and self.filter_timer.isActive():self.filter_changed()
        rows=self.visible_rows() if filtered else semantics.working_rows(self.result['rows'])
        if not rows:QMessageBox.information(self,'没有可导出商品','请调整筛选条件。');return
        folder=Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation))/'补货助手输出'
        name=f'补货结果_{self.result["store"]}_{datetime.now():%Y%m%d_%H%M%S}.xlsx'
        path,_=export_locations.save_file(self,f'导出{len(rows):,}行 · '+('当前筛选' if filtered else '本次全部结果'),str(folder/name),'Excel 工作簿 (*.xlsx)',options=QFileDialog.Option.DontConfirmOverwrite)
        if not path:return
        if not path.lower().endswith('.xlsx'):path+='.xlsx'
        scope=f'当前筛选：{self.mode.currentText()}；{self.warehouse_filter.currentText()}；{self.category_filter.currentText()}；搜索={self.search.text()}；共{len(rows)}行' if filtered else '本次全部结果'
        if filtered and any(c.currentIndex()>0 for c in self.result_facets.fields.values()):scope+='；'+self.result_facets.condition.text()
        self.job(engine.export_xlsx,(self.result,path,rows,scope),self.export_ok)

    def export_ok(self,path):
        self.last_export=path;self.status.setText('导出成功：'+Path(path).name);self.status.setToolTip(path)
        box=QMessageBox(self);box.setWindowTitle('导出完成');box.setText('已保存本次结果。');box.setInformativeText(Path(path).name);box.setDetailedText('保存位置：'+path)
        open_file=box.addButton('打开文件',QMessageBox.ButtonRole.ActionRole);open_dir=box.addButton('打开文件夹',QMessageBox.ButtonRole.ActionRole);box.addButton('完成',QMessageBox.ButtonRole.AcceptRole);box.exec()
        if box.clickedButton()==open_file:QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        elif box.clickedButton()==open_dir:QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path).parent)))

