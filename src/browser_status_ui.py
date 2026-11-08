"""Transport observation plus separately verified startup-login status."""
import time
import shiboken6
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
        startup=getattr(host,'browser_startup',None)
        if startup:
            from ui_controls import button
            row.addWidget(button('连接检查 / 修复',startup.open_details))
            startup.changed.connect(self.render)
        self.timer=QTimer(self);self.timer.setInterval(INTERVAL_MS);self.timer.timeout.connect(self.refresh);self.timer.start()
        self.initial=QTimer(self);self.initial.setSingleShot(True);self.initial.timeout.connect(self.refresh);self.initial.start(0)

    def owner(self,kind):
        manager=getattr(self.host,'aoxiang_workflows' if kind=='ax' else 'catalog_sessions',None)
        return getattr(manager,'session',None)

    def set_state(self,name,state):
        if not shiboken6.isValid(self) or not shiboken6.isValid(self.dots[name]):return
        state=state if state in STATES else 'UNKNOWN';self.states[name]=state
        text,color=STATES[state]
        tip=name+'：'+text+'。约每3秒更新；仅表示浏览器连接，不代表网站登录有效或业务核验通过。'
        startup=getattr(self.host,'browser_startup',None)
        owner=self.owner('ax' if name=='翱象' else 'jst')
        checked=startup.presentation(name,state,getattr(owner,'client',None)) if startup else None
        if checked:
            text,color,detail=checked;tip=name+'：'+detail+'\n登录核验仅代表最近检查时刻；业务执行前仍核验账号与范围。'
        self.dots[name].setStyleSheet(f'color:{color};font-size:12px;')
        self.labels[name].setText(name+' '+text);self.labels[name].setAccessibleName(name+'浏览器连接'+text)
        if state=='DISCONNECTED' and not checked:tip+='\n请点击“连接检查 / 修复”查看原因并重试；原任务不会自动重发。'
        elif state=='UNKNOWN' and not checked:tip+='\n暂未取得可靠状态，不据此停止或重发业务任务。'
        self.labels[name].setToolTip(tip);self.dots[name].setToolTip(tip)

    def render(self):
        if not shiboken6.isValid(self) or self.paused:return
        for name,state in list(self.states.items()):self.set_state(name,state)

    def set_group(self,kind,state):
        for name in NAMES[kind]:self.set_state(name,state)

    def refresh(self):
        if not shiboken6.isValid(self) or self.paused:return
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
                startup=getattr(self.host,'browser_startup',None)
                if k=='ax' and startup and data.get('login_state'):
                    from browser_startup import error_result,result
                    if data.get('login_error'):
                        record=error_result(data['login_error'])
                    elif data['login_state']=='VERIFIED':
                        record=result('READY','当前共享连接的商家身份已核验。')
                    else:
                        record=result('WAITING','当前连接尚未完成身份核验；不能沿用上次登录成功状态。')
                    startup.records['翱象']={**record,'client':c}
                for name,state in states.items():self.set_state(name,state)
            def failed(_text,k=kind,c=client):
                if not self.paused and getattr(self.owner(k),'client',None) is c:self.set_group(k,'UNKNOWN' if c.running else 'DISCONNECTED')
            def finished(k=kind,t=task):
                self.workers.pop(k,None);t.deleteLater()
                if self.close_requested and not self.workers:QTimer.singleShot(0,self.host.close)
            task.succeeded.connect(apply);task.failed.connect(failed);task.finished.connect(finished);task.start()

    def prepare_close(self):
        self.paused=self.close_requested=True;self.timer.stop();self.initial.stop()
        return not self.workers

    def resume(self):
        self.paused=self.close_requested=False;self.timer.start();self.refresh()
