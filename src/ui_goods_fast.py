"""Native Qt sorting/filtering with lazily formatted business cells.

Only one native item per source row is allocated. Its private ranks and pass flag
are presentation data; complete business records remain untouched in source order.
"""
from PySide6.QtCore import Qt,QModelIndex,QSortFilterProxyModel,QSignalBlocker
from PySide6.QtGui import QStandardItemModel,QStandardItem,QColor
from display_fields import COLUMNS
import result_semantics as sem
import new_products

RANK_ROLES=(int(Qt.ItemDataRole.UserRole)+10,int(Qt.ItemDataRole.UserRole)+11)
PASS_ROLE=int(Qt.ItemDataRole.UserRole)+12


class GoodsModel(QStandardItemModel):
    def __init__(self):super().__init__();self.rows=[];self.values={};self.sort_keys={};self.items=[];self.rank_cache={}
    def set_rows(self,rows):
        self.beginResetModel()
        blocker=QSignalBlocker(self)
        try:
            self.clear();self.rows=rows;self.values={};self.sort_keys={};self.rank_cache={};self.items=[]
            self.setColumnCount(len(COLUMNS));self.setRowCount(len(rows));self.setHorizontalHeaderLabels([c[0] for c in COLUMNS])
            for i,d in enumerate(rows):
                item=QStandardItem();item.setData('1',PASS_ROLE);self.setItem(i,0,item);self.items.append(item)
        finally:del blocker;self.endResetModel()
    def column_values(self,column):
        if column not in self.values:self.values[column]=[COLUMNS[column][1](d) for d in self.rows]
        return self.values[column]
    def key(self,row,column):
        if column not in self.sort_keys:self.sort_keys[column]=[(0,v) if sem.numeric(v) else (1,str(v or '')) for v in self.column_values(column)]
        return self.sort_keys[column][row]
    def ranks(self,column):
        if column not in self.rank_cache:
            if self.rows:self.key(0,column)
            keys=self.sort_keys.get(column,[]);ordered=sorted(set(keys));rank={key:i for i,key in enumerate(ordered)};self.rank_cache[column]=[rank[key] for key in keys]
        return self.rank_cache[column]
    def business_data(self,index,role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():return None
        d=self.rows[index.row()]
        if role in (Qt.ItemDataRole.DisplayRole,Qt.ItemDataRole.UserRole,Qt.ItemDataRole.ToolTipRole):
            v=self.column_values(index.column())[index.row()]
            return v if role==Qt.ItemDataRole.UserRole else sem.fmt(v)
        if role==Qt.ItemDataRole.TextAlignmentRole and index.column() in range(5,11):return int(Qt.AlignmentFlag.AlignRight|Qt.AlignmentFlag.AlignVCenter)
        if role==Qt.ItemDataRole.ForegroundRole:
            if sem.exclusion_reason(d) and index.column() in (9,10,13,15,16):return QColor('#9B6D16')
            if d.get('_audit',{}).get('applied') and index.column() in (9,11):return QColor('#13796C')
            if d['_source']=='地采商品' and index.column()==2:return QColor('#9B6D16')


class FilterModel(QSortFilterProxyModel):
    def __init__(self):
        super().__init__();self.mode='全部商品';self.search='';self.warehouse='全部仓';self.category='全部分类';self.column=-1;self.rank_role=0
        self.setDynamicSortFilter(True);self.setFilterKeyColumn(0);self.setFilterRole(PASS_ROLE);self.setFilterFixedString('1');self.setSortRole(RANK_ROLES[0])
    def data(self,index,role=Qt.ItemDataRole.DisplayRole):
        if self.sourceModel() is not None:return self.sourceModel().business_data(self.mapToSource(index),role)
    def setSourceModel(self,model):
        super().setSourceModel(model)
        if model is not None:model.modelReset.connect(self.source_reset)
    def source_reset(self):
        if self.column>=0:self.sort(self.column,self.sortOrder())
    def accepts(self,d):
        if not sem.work_eligible(d):return False
        if self.warehouse!='全部仓' and d.get('AU')!=self.warehouse:return False
        if self.category!='全部分类' and str(d.get('_category') or '未分类')!=self.category:return False
        if self.search and self.search not in (' '.join(str(d.get(k,'')) for k in ['D','E','F'])).lower():return False
        if self.mode in ('动销补货','新品候选','新品推荐','缺货恢复及其他'):return new_products.is_new(d) if self.mode=='新品候选' else new_products.category(d)==self.mode
        if self.mode=='建议补货':return sem.orderable(d)
        if self.mode=='必卖商品':return bool(d.get('_must_sell'))
        if self.mode=='预算未满足':return d.get('_budget_gap',0)>0
        if self.mode=='必卖未补足':return bool(d.get('_must_sell')) and d.get('_budget_gap',0)>0
        if self.mode=='其他预算暂缓':return not d.get('_must_sell') and d.get('_budget_gap',0)>0
        if self.mode=='最低持有缺口':return bool(d.get('_holding_gap'))
        if self.mode in ('已排除','商品资料禁用','补货建议停采'):return False
        if self.mode=='资料匹配':return d['_source']=='资料匹配'
        if self.mode=='一箱例外':return bool(d.get('_audit',{}).get('applied'))
        if self.mode=='地采商品':return d['_source']=='地采商品'
        if self.mode=='待处理':return bool(sem.issue_text(d))
        if self.mode=='重点关注':return bool(d.get('BW') or sem.issue_text(d) or any('停采' in n for n in d['_meta']['notes']))
        return True
    def endFilterChange(self,directions=QSortFilterProxyModel.Direction.Rows):
        model=self.sourceModel()
        if model:
            blocker=QSignalBlocker(model)
            try:
                for d,item in zip(model.rows,model.items):item.setData('1' if self.accepts(d) else '0',PASS_ROLE)
            finally:del blocker
        super().endFilterChange(directions)
    def invalidateFilter(self):self.beginFilterChange();self.endFilterChange()
    def sortColumn(self):return self.column
    def sort(self,column,order=Qt.SortOrder.AscendingOrder):
        model=self.sourceModel();changed=column!=self.column;self.column=column
        if column<0 or model is None:super().sort(-1,order);return
        if changed or not model.rank_cache:
            role=RANK_ROLES[1-self.rank_role];blocker=QSignalBlocker(model)
            try:
                for rank,item in zip(model.ranks(column),model.items):item.setData(rank,role)
            finally:del blocker
            self.rank_role=1-self.rank_role;self.setSortRole(role)
        super().sort(0,order)
