"""Small global indicators. Observe existing transports only; never start/reconnect them."""
import time
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QWidget,QHBoxLayout,QLabel
from aoxiang_ui_common import Call

INTERVAL_MS=3000
TIMEOUT=1.5
STATES={
    'CONNECTED':('已连接','#16805d'),
    'NOT_CONNECTED':('未连接','#73838b'),
    'CONNECTING':('连接中','#a26b12'),
    'DISCONNECTED':('已断开','#ba4242'),
    'UNKNOWN':('待确认','#a26b12'),
}
NAMES={'ax':['翱象'],'jst':['山东仓','河北仓']}


def states_for(kind,data):
    if kind=='ax':
        state=data.get('state','UNKNOWN')
        return {'翱象':state if state in STATES else 'UNKNOWN'}
    items=data.get('connections')
    if not isinstance(items,list):return {name:'UNKNOWN' for name in NAMES[kind]}
    result={name:'NOT_CONNECTED' for name in NAMES[kind]}
    for name in result:
        found=[r for r in items if r.get('warehouse')==name]
        if len(found)>1:result[name]='UNKNOWN'
        elif found:result[name]=found[0].get('state') if found[0].get('state') in STATES else 'UNKNOWN'
    return result


class BrowserStatus(QWidget):
    def __init__(self,host):
        super().__init__(host)
        self.host=host;self.workers={};self.dots={};self.labels={};self.states={}
        self.paused=False;self.close_requested=False
        self.setObjectName('browserConnectionIndicators')
        row=QHBoxLayout(self);row.setContentsMargins(4,0,8,0);row.setSpacing(7)
        caption=QLabel('浏览器连接');caption.setStyleSheet('color:#657981; font-size:12px;');row.addWidget(caption)
        for name in ['翱象','山东仓','河北仓']:
            dot=QLabel('●');dot.setFixedWidth(12);row.addWidget(dot)
            text=QLabel();text.setStyleSheet('color:#425d67; font-size:12px;');text.setWordWrap(False);row.addWidget(text)
            self.dots[name]=dot;self.labels[name]=text;self.set_state(name,'NOT_CONNECTED')
            if name!='河北仓':row.addSpacing(10)
        host.statusBar().setSizeGripEnabled(False)
        host.statusBar().addPermanentWidget(self)
        self.timer=QTimer(self);self.timer.setInterval(INTERVAL_MS);self.timer.timeout.connect(self.refresh);self.timer.start()
        QTimer.singleShot(0,self.refresh)

    def owner(self,kind):
        manager=getattr(self.host,'aoxiang_workflows' if kind=='ax' else 'catalog_sessions',None)
        return getattr(manager,'session',None)

    def set_state(self,name,state):
        state=state if state in STATES else 'UNKNOWN';self.states[name]=state
        text,color=STATES[state];self.dots[name].setStyleSheet(f'color:{color};font-size:12px;')
        self.labels[name].setText(name+' '+text);self.labels[name].setAccessibleName(name+'浏览器连接'+text)
        tip=name+'：'+text+'。约每3秒更新；仅表示浏览器连接，不代表网站登录有效或业务核验通过。\n只读取已有连接，不主动重连或触发授权。'
        if state=='DISCONNECTED':tip+='\n请到对应连接窗口检查浏览器，需要时主动重连；原任务不会自动重发。'
        elif state=='UNKNOWN':tip+='\n暂未取得可靠状态，不据此停止或重发业务任务。'
        self.labels[name].setToolTip(tip);self.dots[name].setToolTip(tip)

    def set_group(self,kind,state):
        for name in NAMES[kind]:self.set_state(name,state)

    def refresh(self):
        if self.paused:return
        for kind in NAMES:
            owner=self.owner(kind);client=getattr(owner,'client',None)
            if client is None:self.set_group(kind,'NOT_CONNECTED');continue
            if not client.running:self.set_group(kind,'DISCONNECTED');continue
            if not getattr(owner,'started' if kind=='ax' else 'ready',False):
                self.set_group(kind,'UNKNOWN');continue
            if kind in self.workers:
                if time.monotonic()-self.workers[kind][2]>INTERVAL_MS/1000+TIMEOUT:self.set_group(kind,'UNKNOWN')
                continue
            # Capture exactly this transport. A late old-process result cannot paint a new one green.
            task=Call(lambda _,c=client:c.request('session.health',timeout=TIMEOUT),self)
            self.workers[kind]=(task,client,time.monotonic())
            def apply(data,k=kind,c=client):
                if self.paused or getattr(self.owner(k),'client',None) is not c:return
                try:states=states_for(k,data)
                except (TypeError,AttributeError):self.set_group(k,'UNKNOWN');return
                for name,state in states.items():self.set_state(name,state)
            def failed(_text,k=kind,c=client):
                if not self.paused and getattr(self.owner(k),'client',None) is c:self.set_group(k,'UNKNOWN' if c.running else 'DISCONNECTED')
            def finished(k=kind,t=task):
                self.workers.pop(k,None);t.deleteLater()
                if self.close_requested and not self.workers:QTimer.singleShot(0,self.host.close)
            task.succeeded.connect(apply);task.failed.connect(failed);task.finished.connect(finished);task.start()

    def prepare_close(self):
        self.paused=self.close_requested=True;self.timer.stop()
        return not self.workers

    def resume(self):
        self.paused=self.close_requested=False;self.timer.start();self.refresh()
