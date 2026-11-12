"""Independent, read-only viewer of one data relocation receipt. Never opens a DB."""
import argparse,json,os,re,sys,time
from datetime import datetime,timezone,timedelta
from pathlib import Path

PHASES={
 'PREPARED':('等待安全退出',0),'WAITING_EXIT':('等待安全退出',0),'WAITING_LOCKS':('核对资料占用',0),
 'COPYING':('复制资料并逐文件核对',1),'CHECKING':('核对文件与数据库',2),
 'TARGET_READY':('切换资料位置',3),'SOURCE_RENAME':('切换资料位置',3),'SWITCH_RECHECK':('切换前最终复核',3),'LINKING':('保留旧路径兼容',3),
 'CONFIG_COMMITTED':('新位置已启用',4),'ARCHIVE_PRECHECK':('核对恢复副本范围',4),'ARCHIVING':('归档完整恢复副本',4),
 'ARCHIVE_VERIFY':('校验恢复归档',4),'CLEANUP_CHECK':('核对可清理范围',5),'CLEANING_OLD':('清理旧位置副本',5),
 'COMPLETED':('搬迁完成',6),'CLEANUP_PENDING':('新位置已启用，旧副本尚待处理',5),'ROLLED_BACK':('未完成，继续使用原位置',0),'RECOVERY_REQUIRED':('需要核对恢复',0),'CANCELLED':('已取消，原位置保持',0)}
STEPS=['安全退出','复制资料','完整性校验','切换位置','恢复副本归档','清理旧副本','完成']

def read_status(control,nonce):
    if not re.fullmatch('[a-f0-9]{32}',nonce):raise ValueError('搬迁操作身份无效')
    path=Path(control)/'operations'/(nonce+'.status.json')
    if not path.is_file():path=Path(control)/'move.json' # Compatibility with 0.52.1.
    if not path.is_file():raise ValueError('尚未读取到本次搬迁记录；不会在这里启动搬迁')
    from data_location_status import read
    value=read(path)
    if value.get('nonce')!=nonce:raise ValueError('当前记录已换为另一项操作；本窗口不自动跟随其他搬迁')
    return value

def presentation(value):
    state=value.get('state','');phase=value.get('phase',state);title,index=PHASES.get(phase,('等待下一步进度',0))
    terminal=state in ('COMPLETED','FAILED','CANCELLED')
    success=state=='COMPLETED' and phase=='COMPLETED'
    if state=='FAILED':title='搬迁未完成：'+('需要核对恢复' if phase=='RECOVERY_REQUIRED' else '继续使用原位置')
    elif state=='CANCELLED':title='已取消，资料位置不变'
    elif state=='COMPLETED' and not success:title='新位置已启用；恢复副本或旧副本清理尚未完成'
    done=value.get('bytes_done');total=value.get('bytes_total');files=value.get('files');total_files=value.get('total_files')
    # Also display an in-flight 0.52.1 job without changing its producer.
    if not isinstance(total,(int,float)) or total<=0:
        old=re.search(r'([\d.]+)/([\d.]+) MiB',value.get('message',''))
        if old:done,total=(float(x)*1024**2 for x in old.groups())
    percent=None;counts=[]
    if isinstance(done,(int,float)) and isinstance(total,(int,float)) and total>0:
        percent=max(0,min(100,done/total*100))
        scale,unit=(1024**3,'GiB') if total>=1024**3 else (1024**2,'MiB')
        counts.append(f'{done/scale:.2f} / {total/scale:.2f} {unit}（本阶段 {percent:.1f}%）')
    elif total is None and isinstance(files,int) and isinstance(total_files,int) and total_files>0:
        percent=max(0,min(100,files/total_files*100))
    if isinstance(files,int) and isinstance(total_files,int):counts.append(f'文件 {files} / {total_files}')
    error=value.get('error') or {};details='\n'.join(str(x) for x in (error.get('message'),value.get('rollback_error'),value.get('reopen_error')) if x)
    return {'state':state,'phase':phase,'title':title,'index':index,'terminal':terminal,'success':success,'percent':percent,'counts':'；'.join(counts),'details':details}

def launch(plan,qa=None,progress=lambda _:None,cancel=None):
    """Confirm an independent window is painted before the writer exits Qt."""
    import subprocess,uuid
    import data_location
    from runtime_support import checkpoint
    checkpoint(cancel);progress('打开独立搬迁进度窗口')
    command=[sys.executable,'--data-location-progress'] if getattr(sys,'frozen',False) else [sys.executable,str(Path(__file__).resolve())]
    token=uuid.uuid4().hex;command+=['--operation',plan['nonce'],'--ready-token',token]
    if qa:command+=['--qa',str(qa[0]),qa[1]]
    folder=data_location.control()/'diagnostics';folder.mkdir(parents=True,exist_ok=True)
    with (folder/('progress-'+plan['nonce']+'.log')).open('ab') as error:
        child=subprocess.Popen(command,cwd=data_location.control(),stdout=subprocess.DEVNULL,stderr=error,env=dict(os.environ),creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    ready=data_location.control()/'observers'/(token+'.json');deadline=time.monotonic()+12
    while not ready.is_file():
        if child.poll() is not None:raise RuntimeError('进度窗口启动失败，未开始搬迁；诊断：'+str(folder))
        if time.monotonic()>deadline:raise RuntimeError('进度窗口没有按时打开，未开始搬迁；请保留原窗口后重试')
        time.sleep(.05)
    value=json.loads(ready.read_text('utf-8'))
    if value.get('type')!='progress_ready' or value.get('operation')!=plan['nonce'] or value.get('token')!=token or child.poll() is not None:raise RuntimeError('进度窗口未就绪，未开始搬迁')
    return child.pid

def dialog(parent,nonce,*,control=None,qa=None):
    from PySide6.QtCore import Qt,QTimer
    from PySide6.QtWidgets import QDialog,QVBoxLayout,QHBoxLayout,QLabel,QLineEdit,QProgressBar,QPushButton,QPlainTextEdit,QSizePolicy
    import data_location
    control=Path(control or data_location.control())
    class View(QDialog):
        def __init__(self):
            super().__init__(parent);self.setWindowTitle('补货助手 · 数据搬迁进度');self.setObjectName('dataLocationProgress');self.resize(790,560)
            self.last=None;self.events=set();self.progress_buckets={};self.qa=qa;self.nonce=nonce;self.control=control
            layout=QVBoxLayout(self)
            def text(message):
                label=QLabel(message);label.setTextFormat(Qt.TextFormat.PlainText);label.setWordWrap(True);label.setSizePolicy(QSizePolicy.Policy.Expanding,QSizePolicy.Policy.Maximum);return label
            self.title=text('正在读取搬迁进度…');self.title.setStyleSheet('font-size:17pt;font-weight:600');layout.addWidget(self.title)
            self.steps=text(' → '.join(STEPS));layout.addWidget(self.steps)
            self.bar=QProgressBar();self.bar.setMinimumHeight(26);layout.addWidget(self.bar)
            self.counts=text('');layout.addWidget(self.counts);self.message=text('');layout.addWidget(self.message)
            self.updated=text('');layout.addWidget(self.updated)
            self.paths={}
            for key,label in [('source','原资料位置'),('target','新资料位置'),('archive','恢复副本位置')]:
                row=QHBoxLayout();row.addWidget(text(label));field=QLineEdit();field.setReadOnly(True);row.addWidget(field,1);layout.addLayout(row);self.paths[key]=field
            self.details=QPlainTextEdit();self.details.setReadOnly(True);self.details.setMaximumHeight(125);self.details.setStyleSheet('QPlainTextEdit {background:#FFFFFF;color:#22383C;border:1px solid #cbdcde;}');layout.addWidget(self.details,1)
            layout.addStretch(1)
            self.note=text('本窗口只查看状态，不打开业务库、不重新搬迁。关闭窗口不会停止搬迁；再次打开补货助手可继续查看。');self.note.setStyleSheet('color:#526c70');layout.addWidget(self.note)
            self.close_button=QPushButton('关闭进度窗口');self.close_button.clicked.connect(self.close);layout.addWidget(self.close_button)
            self.timer=QTimer(self);self.timer.setInterval(500);self.timer.timeout.connect(self.refresh);self.timer.start();self.refresh()
            from table_readability import fit_window
            fit_window(self,790,560)
        def emit(self,event,**values):
            if not self.qa:return
            out,case=self.qa
            with (Path(out)/(case+'-progress-events.jsonl')).open('a',encoding='utf-8') as file:file.write(json.dumps({'event':event,'pid':os.getpid(),**values},ensure_ascii=False)+'\n')
        def refresh(self):
            try:
                value=read_status(self.control,self.nonce);view=presentation(value);self.last=value
                self.title.setText(view['title']);self.title.setStyleSheet('font-size:17pt;font-weight:600;color:'+('#a03428' if view['state']=='FAILED' else '#116c61'))
                self.steps.setText(' → '.join(('✓ ' if i<view['index'] and view['state']!='FAILED' or view['success'] else '● ' if i==view['index'] else '')+name for i,name in enumerate(STEPS)))
                if view['terminal']:self.bar.setRange(0,1000);self.bar.setValue(1000 if view['success'] else 0);self.bar.setFormat('已完成' if view['success'] else '请查看本次结果')
                elif view['percent'] is None:self.bar.setRange(0,0)
                else:self.bar.setRange(0,1000);self.bar.setValue(round(view['percent']*10));self.bar.setFormat('本阶段 %p%')
                self.counts.setText(view['counts']);self.message.setText(value.get('message',''))
                at=value.get('at','');elapsed=''
                try:
                    stamp=datetime.fromisoformat(at);age=max(0,int((datetime.now(timezone.utc)-stamp.astimezone(timezone.utc)).total_seconds()))
                    elapsed=f'（{age}秒前）'
                except (ValueError,TypeError):pass
                self.updated.setText('最近更新：'+at.replace('T',' ')+(elapsed if not view['terminal'] else '')+'\n各阶段工作量不同；本阶段百分比不代表整体已经完成。')
                for key,field in self.paths.items():field.setText(value.get(key,''));field.setCursorPosition(0);field.setToolTip(value.get(key,''))
                self.details.setPlainText(view['details']);self.details.setVisible(bool(view['details']));self.layout().activate()
                if view['state']=='COMPLETED' and not view['success']:self.note.setText('新位置已经启用；完整恢复副本或原位置副本保留。请在“数据保存位置”继续归档、核验并清理。')
                key=(view['state'],view['phase'])
                if key not in self.events:
                    self.events.add(key);self.emit('displayed',state=view['state'],phase=view['phase'],title=view['title'],percent=view['percent'],counts=view['counts'])
                    if self.qa:
                        name=self.qa[1]+'-progress-'+view['phase']+'.png'
                        QTimer.singleShot(100,self,lambda:self.grab().save(str(Path(self.qa[0])/name)))
                if self.qa and view['percent'] is not None and 0<view['percent']<100:
                    bucket=int(view['percent']//10)
                    if self.progress_buckets.get(view['phase'])!=bucket:
                        self.progress_buckets[view['phase']]=bucket;self.emit('advanced',phase=view['phase'],percent=view['percent'],counts=view['counts'])
                        target=str(Path(self.qa[0])/(self.qa[1]+'-progress-'+view['phase']+'.png'))
                        QTimer.singleShot(50,self,lambda target=target:self.grab().save(target))
                if self.qa and (view['terminal'] or self.qa[1]=='busy-second') and 'closing' not in self.events:
                    self.events.add('closing');QTimer.singleShot(1200,self,self.close)
            except (OSError,ValueError,TypeError,KeyError) as exc:
                self.title.setText('暂未读取到最新进度');self.message.setText(str(exc));self.bar.setRange(0,0)
                self.updated.setText('本窗口没有启动、重试或中止任何搬迁。')
        def closeEvent(self,event):self.timer.stop();self.emit('closed');super().closeEvent(event)
    return View()

def main(args=None):
    parser=argparse.ArgumentParser();parser.add_argument('--operation',required=True);parser.add_argument('--qa',nargs=2);parser.add_argument('--ready-token');a=parser.parse_args(args)
    if not re.fullmatch('[a-f0-9]{32}',a.operation):raise ValueError('搬迁操作身份无效')
    if a.ready_token and not re.fullmatch('[a-f0-9]{32}',a.ready_token):raise ValueError('进度窗口身份无效')
    qa=None
    if a.qa:
        from data_location_smoke import context
        out,case,_=context(a.qa);qa=(out,case)
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QTimer
    app=QApplication(['数据搬迁进度']);app.setApplicationName('补货助手搬迁进度');app.setStyle('Fusion')
    from ui_controls import STYLE
    app.setStyleSheet(STYLE);view=dialog(None,a.operation,qa=qa)
    def exception(kind,value,tb):
        # A windowed EXE has no stderr. Preserve original callback exceptions
        # without creating an error widget, including during Qt finalization.
        import traceback
        detail=''.join(traceback.format_exception(kind,value,tb))
        try:
            folder=view.control/'diagnostics';folder.mkdir(parents=True,exist_ok=True)
            with (folder/'progress-errors.jsonl').open('a',encoding='utf-8') as file:file.write(json.dumps({'operation':a.operation,'error':detail},ensure_ascii=False)+'\n')
            view.emit('exception',traceback=detail)
        except OSError:pass
    sys.excepthook=exception
    view.show();app.aboutToQuit.connect(view.timer.stop)
    def ready():
        view.emit('opened',visible=view.isVisible(),controls_fit=view.rect().contains(view.close_button.geometry()),control=str(view.control),operation=a.operation)
        if a.ready_token:
            from stockout_common import atomic_json
            atomic_json(view.control/'observers'/(a.ready_token+'.json'),{'type':'progress_ready','operation':a.operation,'token':a.ready_token,'pid':os.getpid()})
    QTimer.singleShot(0,view,ready)
    return app.exec()

def show_pending(app):
    """Busy startup stays read-only: no lease, repository, recovery or apply."""
    import data_location
    if os.environ.get('REPLENISHMENT_DATA_DIR'):return None
    path=data_location.journal_file()
    if not path.is_file():return None
    from data_location_status import read
    value=read(path)
    if value.get('state') not in ('WAITING_EXIT','APPLYING'):return None
    qa=None
    if '--data-location-smoke' in sys.argv:
        from data_location_smoke import context
        index=sys.argv.index('--data-location-smoke');out,case,_=context(sys.argv[index+1:]);qa=(out,case)
    launch(value,qa=qa)
    return 0


if __name__=='__main__':raise SystemExit(main())
