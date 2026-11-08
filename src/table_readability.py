"""Display-only table sizing and full text access, bounded by visible metadata."""
from PySide6.QtCore import QObject, QEvent, QTimer, Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import QApplication, QMenu, QHeaderView


def fit_window(window, width=1100, height=740):
    screen = window.screen() or QApplication.primaryScreen()
    if screen is None:return
    area = screen.availableGeometry()
    width, height = min(width, max(320, area.width()-32)), min(height, max(240, area.height()-64))
    window.setMinimumSize(min(720,width), min(400,height))
    window.resize(width,height)
    window.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint,True)


def row_values(table):
    index = table.currentIndex();model = table.model()
    if not index.isValid() or model is None:return [],[]
    headers = [str(model.headerData(c,Qt.Orientation.Horizontal) or c+1) for c in range(model.columnCount())]
    values = [model.data(model.index(index.row(),c),Qt.ItemDataRole.DisplayRole) for c in range(model.columnCount())]
    return headers,values


def full_row(table):
    headers,values = row_values(table)
    if not headers:return
    from readable_ui import details
    details(table,'当前行完整内容',['字段','内容'],[[h,'' if v is None else v] for h,v in zip(headers,values)],
            '只读查看；不改变筛选、勾选、修改数量或风险确认。')


class TableReading(QObject):
    def __init__(self,table):
        super().__init__(table);self.table=table;self.last_size=None;self.headers=None;self.applying=False;self.generation=None;self.key=None
        self.timer=QTimer(self);self.timer.setSingleShot(True);self.timer.timeout.connect(self.apply)
        self.save_timer=QTimer(self);self.save_timer.setSingleShot(True);self.save_timer.setInterval(500);self.save_timer.timeout.connect(self.save_widths)
        table.horizontalHeader().sectionResized.connect(self.width_changed)
        table.installEventFilter(self);table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        table.customContextMenuRequested.connect(self.menu)
        action=QAction('查看当前行完整内容',table);action.setShortcut(QKeySequence('Ctrl+Shift+D'));action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        action.triggered.connect(lambda:full_row(table));table.addAction(action)
        if table.model():table.model().modelReset.connect(self.schedule)
        self.schedule()
    def schedule(self,*_):self.timer.start(0)
    def width_changed(self,*_):
        if not self.applying and self.key:self.save_timer.start()
    def save_widths(self):
        app=QApplication.instance();settings=getattr(app,'reading_settings',None)
        if settings is None or not self.key:return
        tables=settings.setdefault('tables',{})
        if len(tables)>=100 and self.key not in tables:tables.pop(next(iter(tables)))
        tables[self.key]=[self.table.columnWidth(i) for i in range(self.table.model().columnCount())]
        try:__import__('display_settings').save()
        except OSError:pass
    def eventFilter(self,obj,event):
        if event.type() in (QEvent.Type.FontChange,QEvent.Type.StyleChange,QEvent.Type.Show):self.schedule()
        return False
    def apply(self):
        table=self.table
        from shiboken6 import isValid
        if not isValid(table):return
        model=table.model()
        if model is None:return
        self.applying=True
        app=QApplication.instance();generation=app.property('reading_layout_generation')
        if self.generation!=generation:self.headers=None;self.generation=generation
        fm=table.fontMetrics();size=max(1,fm.height());density=app.property('reading_density') or 'standard'
        padding={'compact':8,'standard':14,'roomy':22}.get(density,14)
        table.verticalHeader().setDefaultSectionSize(max(30,size+padding))
        table.horizontalHeader().setMinimumHeight(size+18)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        table.setWordWrap(False);table.setTextElideMode(Qt.TextElideMode.ElideRight)
        headers=tuple(str(model.headerData(c,Qt.Orientation.Horizontal) or '') for c in range(model.columnCount()))
        if headers!=self.headers:
            import hashlib,json
            self.key=type(table.window()).__name__+':'+(table.objectName() or hashlib.sha256(json.dumps(headers,ensure_ascii=False).encode()).hexdigest()[:16])
            saved=getattr(app,'reading_settings',{}).get('tables',{}).get(self.key,[])
            if not isinstance(saved,list):saved=[]
            for c,title in enumerate(headers):
                units=28 if any(k in title for k in ('原因','说明','风险','内容','依据','路径')) else 22 if any(k in title for k in ('名称','单号','原值','新值')) else 16 if any(k in title for k in ('编码','时间','日期','参数')) else 12
                width=min(480,max(70,units*fm.averageCharWidth(),fm.horizontalAdvance(title)+24))
                if len(saved)==len(headers) and type(saved[c]) is int:width=min(1600,max(60,saved[c]))
                table.setColumnWidth(c,width)
            self.headers=headers
        elif self.last_size and self.last_size!=size:
            for c in range(model.columnCount()):table.setColumnWidth(c,max(60,round(table.columnWidth(c)*size/self.last_size)))
        self.last_size=size;self.applying=False
    def menu(self,point):
        menu=QMenu(self.table);menu.addAction('查看当前行完整内容',lambda:full_row(self.table))
        def copy():
            hs,values=row_values(self.table)
            QApplication.clipboard().setText('\n'.join(f'{h}：{v if v is not None else ""}' for h,v in zip(hs,values)))
        menu.addAction('复制当前行全部字段',copy);menu.exec(self.table.viewport().mapToGlobal(point))


def install(table):
    if not hasattr(table,'reading_controller'):table.reading_controller=TableReading(table)
    return table.reading_controller
