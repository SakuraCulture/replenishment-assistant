"""Bounded best-effort local performance evidence, independent of business data.

Only the GUI process owns a writer. Producers never perform disk IO or wait for
the bounded queue. No argument, progress text, result, SQL, or exception message
is retained. Sampling is low frequency; operation summaries cover short tasks.
"""
import functools
import json
import os
import queue
import re
import threading
import time
import uuid
import weakref
from pathlib import Path

INTERVAL=5.0
RETENTION=24*60*60
MAX_BYTES=16*1024**2
FILE_BYTES=1024**2
MAX_QUEUE=256
MAX_ACTIVE=32
MAX_PROCESSES=16
FILE_RE=re.compile(r'perf-v1-\d+-\d+-[a-f0-9]{12}-\d+\.jsonl\Z')
NAME_RE=re.compile(r'[a-zA-Z_][a-zA-Z0-9_]{0,63}(?:\.[a-zA-Z_][a-zA-Z0-9_]{0,63}){0,2}\Z')
PAYMENT_COUNTS={'candidates','selected','signed_cache_hits','signed_cache_misses','generation_cache_hits',
                'generation_cache_misses','generation_prefilter_skipped'}
_current=None


def safe_name(value):
    return value if type(value) is str and NAME_RE.fullmatch(value) else 'task'


def callable_name(fn):
    return safe_name(getattr(fn,'__module__','')+'.'+getattr(fn,'__name__',''))


def phase_of(message):
    if hasattr(message,'active'):return 'commit' if message.active else 'post_commit'
    text=str(message)[:512]
    if '等待' in text and ('锁' in text or '数据库' in text):return 'wait_database_lock'
    if '已取得写锁' in text:return 'transaction_prepare'
    if '传输' in text or '发送' in text:return 'ipc_send'
    if '接收' in text:return 'ipc_receive'
    if '编码' in text:return 'encoding'
    if '压缩' in text:return 'compression'
    if '备份' in text:return 'backup'
    if '核验文件' in text:return 'file_validation'
    if '已写' in text or '写出' in text:return 'file_write'
    if '读取' in text or '解析' in text or '还原' in text:return 'read_decode'
    if '校验' in text or '核对' in text:return 'validation'
    if '保存' in text or '归档' in text:return 'database_or_archive'
    if '计算' in text:return 'calculation'
    if '浏览器' in text or '登录' in text or '连接' in text:return 'browser_connection'
    if '平台' in text or '查询' in text:return 'platform_wait'
    return 'processing'


class NullSpan:
    def progress(self,*_):pass
    def payment_counts(self,*_):pass
    def __enter__(self):return self
    def __exit__(self,*_):return False


NULL=NullSpan()


class Span:
    def __init__(self,monitor,identifier):self.monitor=monitor;self.identifier=identifier
    def progress(self,message):
        try:self.monitor.progress(self.identifier,message)
        except Exception:pass
    def payment_counts(self,counts):
        try:self.monitor.payment_counts(self.identifier,counts)
        except Exception:pass
    def __enter__(self):return self
    def __exit__(self,t,value,tb):
        try:self.monitor.end(self.identifier,t)
        except Exception:pass
        return False


def begin(operation):
    try:
        if _current is not None:return _current.begin(operation)
    except Exception:pass
    return NULL


def owned(process,role):
    try:
        if _current is not None:_current.owned(process,role)
    except Exception:pass


def work_io(fn):
    @functools.wraps(fn)
    def measured(self,operation,value,progress):
        with begin('workio.'+safe_name(operation)) as task:
            def report(message):task.progress(message);return progress(message)
            return fn(self,operation,value,report)
    return measured


def component(fn):
    @functools.wraps(fn)
    def measured(self,action,*args,**kwargs):
        with begin(safe_name(fn.__module__)+'.'+safe_name(action)):
            return fn(self,action,*args,**kwargs)
    return measured


def log_files(folder):
    root=Path(folder).resolve()
    if not root.is_dir():return []
    return [p for p in root.iterdir() if FILE_RE.fullmatch(p.name) and not p.is_symlink() and p.is_file()]


class Monitor:
    def __init__(self,folder,*,interval=INTERVAL,counters=None):
        self.folder=Path(folder);self.interval=interval;self.counters=counters
        self.session=uuid.uuid4().hex[:12];self.pid=os.getpid();self.events=queue.Queue(MAX_QUEUE)
        self.lock=threading.Lock();self.active={};self.processes=[];self.previous={};self.system_previous=None
        self.stopped=threading.Event();self.thread=None;self.dropped=0;self.write_errors=0;self.sample_errors=0
        self.wake=threading.Event();self.flush_requests=queue.Queue(8)
        self.heartbeat_at=None;self.gui_lag=0.0;self.sequence=0;self.target=None;self.target_size=0
        self.prune_at=0.0;self.counter=0
        self.cpu_seconds=0.0;self.written_bytes=0;self.sample_count=0

    def emit(self,kind,**values):
        if self.stopped.is_set():return
        try:self.events.put_nowait({'at':time.time(),'kind':kind,'session':self.session,**values})
        except queue.Full:self.dropped+=1

    def start(self):
        self.emit('session',schema=1,pid=self.pid,interval_seconds=self.interval,retention_hours=24,max_bytes=MAX_BYTES,
                  process_coverage='main and at most 16 directly owned Python/component processes; external browsers excluded')
        self.thread=threading.Thread(target=self.run,name='light-performance-log',daemon=True);self.thread.start()
        return self

    def stop(self):
        self.stopped.set();self.wake.set()
        if self.thread is not None and self.thread is not threading.current_thread():self.thread.join(.5)

    def flush(self,timeout=2):
        """Only an explicit export waits; normal business producers never wait."""
        if self.thread is None or not self.thread.is_alive() or self.stopped.is_set():return False
        done=threading.Event();result=[]
        try:self.flush_requests.put_nowait((done,result))
        except queue.Full:return False
        self.wake.set()
        return done.wait(timeout) and bool(result and result[0])

    def heartbeat(self):
        now=time.monotonic()
        with self.lock:
            if self.heartbeat_at is not None:self.gui_lag=max(self.gui_lag,max(0,now-self.heartbeat_at-1))
            self.heartbeat_at=now

    def begin(self,operation):
        now=time.monotonic()
        with self.lock:
            if self.stopped.is_set() or len(self.active)>=MAX_ACTIVE:return NULL
            self.counter+=1;identifier=str(self.counter)
            self.active[identifier]={'id':identifier,'operation':safe_name(operation),'started':now,'phase':'prepare',
                                     'phase_at':now,'durations':{},'last_event':now,'current':None,'total':None}
        self.emit('operation_start',id=identifier,operation=safe_name(operation))
        return Span(self,identifier)

    def progress(self,identifier,message):
        phase=phase_of(message);now=time.monotonic();event=None
        with self.lock:
            task=self.active.get(identifier)
            if task is None:return
            for key in ('current','total'):
                value=getattr(message,key,None)
                if type(value) is int and 0<=value<=2**53:task[key]=value
            if phase!=task['phase']:
                previous=task['phase'];elapsed=now-task['phase_at']
                task['durations'][previous]=task['durations'].get(previous,0)+elapsed
                task.update(phase=phase,phase_at=now)
                if now-task['last_event']>=1:
                    task['last_event']=now;event={'id':identifier,'phase':phase,'previous':previous,'previous_seconds':round(elapsed,6)}
        if event:self.emit('phase',**event)

    def end(self,identifier,error_type=None):
        now=time.monotonic()
        with self.lock:
            task=self.active.pop(identifier,None)
        if task is None:return
        durations=task['durations'];phase=task['phase'];durations[phase]=durations.get(phase,0)+now-task['phase_at']
        error=safe_name(getattr(error_type,'__name__','')) if error_type else None
        self.emit('operation_end',id=identifier,operation=task['operation'],seconds=round(now-task['started'],6),
                  outcome='cancelled' if error=='Cancelled' else 'failed' if error else 'ok',error_type=error,
                  phases={k:round(v,6) for k,v in durations.items()},current=task['current'],total=task['total'],
                  **({'payment_index':task['payment_index']} if 'payment_index' in task else {}))

    def payment_counts(self,identifier,counts):
        with self.lock:
            task=self.active.get(identifier)
            if task is not None and task['operation'] in ('payment_matching.index','payment_matching.candidate_snapshots'):
                task['payment_index']=payment_counts(counts)

    def owned(self,process,role):
        if role not in ('work-io','aoxiang-component','jst-component'):role='python-worker'
        def live(ref):
            item=ref()
            if item is None:return False
            try:return item.is_alive() if hasattr(item,'is_alive') else item.poll() is None
            except (OSError,ValueError,AssertionError):return False
        with self.lock:
            self.processes=[v for v in self.processes if live(v[0])]
            if len(self.processes)<MAX_PROCESSES and not any(ref() is process for ref,_ in self.processes):
                self.processes.append((weakref.ref(process),role))

    def sample(self):
        import multiprocessing
        now=time.monotonic()
        with self.lock:
            owned=[(ref(),role) for ref,role in self.processes if ref() is not None]
            active=[{k:t[k] for k in ('id','operation','phase','current','total')}|{'elapsed_seconds':round(now-t['started'],3)} for t in self.active.values()]
            lag=max(self.gui_lag,max(0,now-self.heartbeat_at-1) if self.heartbeat_at is not None else 0)
            self.gui_lag=0.0
        known={id(p) for p,_ in owned}
        owned.extend((p,'python-worker') for p in multiprocessing.active_children() if id(p) not in known)
        processes=[{'role':'main',**self.counters.process()}];limited=max(0,len(owned)-MAX_PROCESSES)
        for process,role in owned[:MAX_PROCESSES]:
            try:
                if hasattr(process,'is_alive'):
                    if not process.is_alive():continue
                    handle=process.sentinel
                else:
                    if process.poll() is not None:continue
                    handle=getattr(process,'_handle',None)
                record=self.counters.process(process.pid,handle)
                if 'identity' in record:processes.append({'role':role,**record})
            except (OSError,ValueError,AssertionError):continue
        previous={}
        for p in processes:
            key=(p['pid'],p.get('identity'));old=self.previous.get(key)
            if old and now>old[0]:
                seconds=now-old[0]
                if 'cpu_seconds' in p and 'cpu_seconds' in old[1]:
                    delta=p['cpu_seconds']-old[1]['cpu_seconds']
                    if delta>=0:p['cpu_cores']=round(delta/seconds,4)
                for n in ('read_bytes','write_bytes'):
                    if n in p and n in old[1] and p[n]>=old[1][n]:p[n+'_per_second']=round((p[n]-old[1][n])/seconds,1)
            previous[key]=(now,dict(p))
        self.previous=previous
        system=self.counters.system();old=self.system_previous
        if old and system.get('cpu_total_ticks',0)>old.get('cpu_total_ticks',0):
            total=system['cpu_total_ticks']-old['cpu_total_ticks'];idle=system['cpu_idle_ticks']-old['cpu_idle_ticks']
            system['cpu_percent']=round(100*max(0,min(total,total-idle))/total,2)
        self.system_previous=system.copy()
        for key in ('cpu_idle_ticks','cpu_total_ticks'):system.pop(key,None)
        return {'at':time.time(),'kind':'sample','session':self.session,'processes':processes,'system':system,
                'active':active,'gui_lag_seconds':round(lag,3),'owned_processes_omitted':limited,
                'sampler_cpu_seconds':round(self.cpu_seconds,6),'written_log_bytes':self.written_bytes,
                'dropped_events':self.dropped,'write_errors':self.write_errors,'sample_errors':self.sample_errors}

    def prune(self,now):
        files=[]
        for p in log_files(self.folder):
            try:
                stat=p.stat()
                if stat.st_mtime<now-RETENTION:p.unlink()
                else:files.append((stat.st_mtime,p,stat.st_size))
            except OSError:pass
        size=sum(n for _,_,n in files)
        # Reserve a full segment, including bytes queued by this flush.
        for _,p,n in sorted(files):
            if size<=MAX_BYTES-FILE_BYTES:break
            if p==self.target:continue
            try:p.unlink();size-=n
            except OSError:pass

    def write(self,records):
        if not records:return
        now=time.time();self.folder.mkdir(parents=True,exist_ok=True)
        data=''.join(json.dumps(r,ensure_ascii=False,separators=(',',':'))+'\n' for r in records).encode('utf-8')
        if len(data)>FILE_BYTES:
            self.dropped+=len(records);return
        if self.target is None or self.target_size+len(data)>FILE_BYTES or not self.target.exists():
            self.sequence+=1;self.target=self.folder/f'perf-v1-{int(now)}-{self.pid}-{self.session}-{self.sequence}.jsonl';self.target_size=0
            self.prune(now);self.prune_at=now
        elif now-self.prune_at>=300:self.prune(now);self.prune_at=now
        with self.target.open('ab') as f:f.write(data)
        self.target_size+=len(data);self.written_bytes+=len(data)

    def run(self):
        started_cpu=time.thread_time()
        try:
            if self.counters is None:
                from performance_counters import Counters
                self.counters=Counters()
            next_sample=time.monotonic()
            while True:
                self.wake.clear();records=[];acks=[]
                for _ in range(8):
                    try:acks.append(self.flush_requests.get_nowait())
                    except queue.Empty:break
                for _ in range(MAX_QUEUE):
                    try:records.append(self.events.get_nowait())
                    except queue.Empty:break
                now=time.monotonic()
                if now>=next_sample and not self.stopped.is_set():
                    try:records.append(self.sample());self.sample_count+=1
                    except Exception:self.sample_errors+=1
                    next_sample=now+self.interval
                closing=self.stopped.is_set()
                if closing:
                    # Stop can race the first drain/sample or a previous disk
                    # write. Drain once more before the final marker; a later
                    # stop always gets another loop, never a premature return.
                    for _ in range(MAX_QUEUE):
                        try:records.append(self.events.get_nowait())
                        except queue.Empty:break
                    records.append({'at':time.time(),'kind':'stopped','session':self.session,'dropped_events':self.dropped,
                                    'write_errors':self.write_errors,'sample_errors':self.sample_errors,
                                    'sampler_cpu_seconds':round(time.thread_time()-started_cpu,6),'written_log_bytes':self.written_bytes})
                saved=True
                try:self.write(records)
                except Exception:self.write_errors+=1;saved=False
                for done,result in acks:result.append(saved);done.set()
                self.cpu_seconds=time.thread_time()-started_cpu
                if closing:return
                self.wake.wait(max(.05,min(self.interval,next_sample-time.monotonic())))
        except Exception:self.sample_errors+=1


def install(runtime,app):
    global _current
    monitor=None
    try:
        from PySide6.QtCore import QTimer
        from data_location import control
        # Small machine-local diagnostics stay outside the movable data tree:
        # even a stalled diagnostic disk write cannot hold its directory open.
        monitor=Monitor(control()/'diagnostics/performance').start();_current=monitor;runtime.performance=monitor
        timer=QTimer(app);timer.setInterval(1000);timer.timeout.connect(monitor.heartbeat);timer.start()
        runtime.performance_timer=timer
        return monitor
    except Exception:
        if monitor is not None:monitor.stop()
        _current=None
        return None


PHASES={'prepare','commit','post_commit','wait_database_lock','transaction_prepare','ipc_send','ipc_receive',
        'encoding','compression','backup','file_validation','file_write','read_decode','validation',
        'database_or_archive','calculation','browser_connection','platform_wait','processing'}
PROCESS_NUMBERS={'pid','identity','cpu_seconds','private_bytes','working_bytes','read_bytes','write_bytes',
                 'cpu_cores','read_bytes_per_second','write_bytes_per_second'}
SYSTEM_NUMBERS={'logical_cpus','physical_total_bytes','physical_available_bytes','commit_total_bytes','commit_limit_bytes','cpu_percent'}


def numbers(value,keys):
    import math
    if not isinstance(value,dict):return {}
    return {k:v for k,v in value.items() if k in keys and type(v) in (int,float) and math.isfinite(v) and v>=0}


def payment_counts(value):
    if not isinstance(value,dict):return {}
    return {k:v for k,v in value.items() if k in PAYMENT_COUNTS and type(v) is int and 0<=v<=2**53}


def clean_record(raw):
    """Export an explicit numeric/enum schema, never arbitrary stored fields."""
    import math
    if not isinstance(raw,dict) or type(raw.get('at')) not in (int,float) or not math.isfinite(raw['at']):return None
    kind=raw.get('kind');session=raw.get('session')
    if kind not in ('session','operation_start','operation_end','phase','sample','stopped'):return None
    if type(session) is not str or not re.fullmatch('[a-f0-9]{12}',session):return None
    result={'at':raw['at'],'kind':kind,'session':session}
    if kind in ('operation_start','operation_end','phase'):
        identifier=raw.get('id')
        if type(identifier) is not str or not re.fullmatch('[0-9]{1,16}',identifier):return None
        result['id']=identifier
    if kind in ('operation_start','operation_end'):result['operation']=safe_name(raw.get('operation'))
    if kind=='operation_end':
        result.update(numbers(raw,{'seconds','current','total'}))
        result['outcome']=raw.get('outcome') if raw.get('outcome') in ('ok','cancelled','failed') else 'unknown'
        result['error_type']=safe_name(raw.get('error_type')) if raw.get('error_type') is not None else None
        result['phases']=numbers(raw.get('phases'),PHASES)
        if result['operation'] in ('payment_matching.index','payment_matching.candidate_snapshots') and 'payment_index' in raw:
            result['payment_index']=payment_counts(raw['payment_index'])
    elif kind=='phase':
        result.update(numbers(raw,{'previous_seconds'}))
        for key in ('phase','previous'):result[key]=raw.get(key) if raw.get(key) in PHASES else 'processing'
    elif kind=='session':result.update(numbers(raw,{'schema','pid','interval_seconds','retention_hours','max_bytes'}))
    elif kind=='sample':
        result.update(numbers(raw,{'gui_lag_seconds','owned_processes_omitted','dropped_events','write_errors','sample_errors','sampler_cpu_seconds','written_log_bytes'}))
        result['system']=numbers(raw.get('system'),SYSTEM_NUMBERS);result['processes']=[];result['active']=[]
        for p in raw.get('processes',[])[:MAX_PROCESSES+1]:
            if not isinstance(p,dict):continue
            role=p.get('role')
            if role not in ('main','python-worker','work-io','aoxiang-component','jst-component'):role='python-worker'
            result['processes'].append({'role':role,**numbers(p,PROCESS_NUMBERS)})
        for p in raw.get('active',[])[:MAX_ACTIVE]:
            if not isinstance(p,dict):continue
            result['active'].append({'operation':safe_name(p.get('operation')),
                'phase':p.get('phase') if p.get('phase') in PHASES else 'processing',
                **numbers(p,{'elapsed_seconds','current','total'})})
    else:result.update(numbers(raw,{'dropped_events','write_errors','sample_errors','sampler_cpu_seconds','written_log_bytes'}))
    return result


def environment():
    import platform
    from version_info import diagnostic
    info=diagnostic()
    # Build diagnostics also contain manifests/paths: only portable identifiers.
    result={k:v for k,v in info.items() if k in ('version','model_version','build_id','source_commit','source_snapshot_sha256')
            and type(v) is str and len(v)<=128 and re.fullmatch(r'[a-zA-Z0-9._ -]+',v)}
    return {**result,'python':platform.python_version(),'bits':64 if __import__('sys').maxsize>2**32 else 32,
            'system':platform.system(),'logical_cpus':os.cpu_count(),'sample_seconds':INTERVAL,
            'retention_hours':24,'maximum_log_bytes':MAX_BYTES}


EXPORT_HELP='''轻量性能日志（近24小时）
默认开启，每5秒采样；阶段事件异步写入。原始日志最多16MiB，定期清理超过24小时的文件。
导出严格只取近24小时有效记录。日志写入失败或队列满时丢弃诊断记录，不阻塞业务。
只包含版本、阶段耗时、数量进度计数、CPU/内存/逻辑I/O与界面心跳延迟。付款索引还记录候选数量及快照缓存命中、未命中计数。
不含商品、门店、金额、单号、文件路径、SQL、请求内容、登录信息或原始错误文字。
CPU cores=1表示用满一个逻辑核心；系统CPU百分比是全机口径。
private_bytes为私有提交，working_bytes为驻留；多进程驻留相加可能重复计算共享页。
read/write bytes为进程逻辑I/O，包含管道等，不等于物理磁盘吞吐或磁盘等待。
只监控主程序及至多16个直接持有的Python/组件子进程，不扫描全机或外部Edge浏览器。
5秒采样可能漏掉短峰值；短操作仍记录总耗时。进度文案只归为固定阶段，不保存原文。
阶段汇总按操作内部计时；并发或嵌套操作不可相加当成全程总时间。
进度更新和心跳可辅助区分CPU计算、资源紧张或等待；不能凭单个指标认定根因。
日志缺失、丢弃及错误计数见摘要。崩溃/断电可能丢失最后约5秒未落盘记录。
请将整个ZIP发给排查人员，无需提供业务数据库。
'''


def export(folder,destination,progress=lambda _:None,cancel=None):
    """Stream a bounded, redacted 24h snapshot into a new ZIP; no database IO."""
    import heapq
    import tempfile
    import zipfile
    from runtime_support import checkpoint
    target=Path(destination)
    if target.suffix.lower()!='.zip':raise ValueError('请使用ZIP文件名')
    if target.exists():raise FileExistsError('请使用一个尚不存在的性能日志文件名')
    now=time.time();selected=[];remaining=MAX_BYTES;flush=None
    monitor=_current
    if monitor is not None and monitor.folder.resolve()==Path(folder).resolve():flush=monitor.flush()
    for p in sorted(log_files(folder),key=lambda p:p.stat().st_mtime,reverse=True):
        stat=p.stat()
        if stat.st_mtime<now-RETENTION:continue
        size=min(stat.st_size,remaining)
        if size:selected.append((p,size));remaining-=size
        if not remaining:break
    summary={'records':0,'samples':0,'completed_operations':0,'failed_operations':0,'cancelled_operations':0,
             'invalid_or_outside_24h':0,'phase_seconds':{},'recent_flush_succeeded':flush,'peak_tree_private_bytes':None,'peak_tree_working_bytes':None,
             'maximum_gui_lag_seconds':None,'minimum_system_physical_available_bytes':None,'incomplete_logging_observed':False}
    if monitor is not None and monitor.folder.resolve()==Path(folder).resolve():
        summary['current_writer']=numbers(vars(monitor),{'dropped','write_errors','sample_errors','cpu_seconds','written_bytes'})
        summary['incomplete_logging_observed']=bool(monitor.dropped or monitor.write_errors or monitor.sample_errors or
            (flush is False and not monitor.stopped.is_set() and monitor.thread is not None and monitor.thread.is_alive()))
    slowest=[];sequence=0;temporary=None
    try:
        fd,temporary=tempfile.mkstemp(prefix='.performance-',suffix='.zip',dir=target.parent);os.close(fd)
        with zipfile.ZipFile(temporary,'w',zipfile.ZIP_DEFLATED,compresslevel=1) as z:
            z.writestr('说明.txt',EXPORT_HELP);z.writestr('环境.json',json.dumps(environment(),ensure_ascii=False,indent=2))
            with z.open('性能日志.jsonl','w') as output:
                for p,size in reversed(selected):
                    checkpoint(cancel);progress('导出近24小时轻量性能记录…')
                    with p.open('rb') as f:
                        while size>0:
                            checkpoint(cancel);line=f.readline(min(size,65537));size-=len(line)
                            if not line:break
                            try:
                                raw=json.loads(line);record=clean_record(raw)
                                if record is None or not now-RETENTION<=record['at']<=now:raise ValueError('outside window')
                            except (ValueError,TypeError,AttributeError,OverflowError):summary['invalid_or_outside_24h']+=1;continue
                            output.write((json.dumps(record,ensure_ascii=False,separators=(',',':'))+'\n').encode('utf-8'))
                            summary['records']+=1
                            if record['kind']=='operation_end':
                                summary['completed_operations']+=1;sequence+=1
                                summary['failed_operations']+=record['outcome']=='failed';summary['cancelled_operations']+=record['outcome']=='cancelled'
                                for phase,seconds in record['phases'].items():summary['phase_seconds'][phase]=summary['phase_seconds'].get(phase,0)+seconds
                                item=(record.get('seconds',0),sequence,record)
                                if len(slowest)<20:heapq.heappush(slowest,item)
                                elif item[:2]>slowest[0][:2]:heapq.heapreplace(slowest,item)
                            elif record['kind']=='sample':
                                summary['samples']+=1
                                for name in ('private_bytes','working_bytes'):
                                    values=[p[name] for p in record['processes'] if name in p]
                                    if values:
                                        key='peak_tree_'+name;summary[key]=max(summary[key] or 0,sum(values))
                                if 'gui_lag_seconds' in record:summary['maximum_gui_lag_seconds']=max(summary['maximum_gui_lag_seconds'] or 0,record['gui_lag_seconds'])
                                available=record['system'].get('physical_available_bytes')
                                if available is not None:
                                    old=summary['minimum_system_physical_available_bytes'];summary['minimum_system_physical_available_bytes']=available if old is None else min(old,available)
                            if any(record.get(k,0)>0 for k in ('dropped_events','write_errors','sample_errors','owned_processes_omitted')):summary['incomplete_logging_observed']=True
            summary['slowest_completed_operations']=[r for _,_,r in sorted(slowest,reverse=True)]
            summary.update(window_hours=24,source_bytes_limit=MAX_BYTES,source_files=len(selected),created_at=now)
            z.writestr('摘要.json',json.dumps(summary,ensure_ascii=False,indent=2))
        checkpoint(cancel);os.rename(temporary,target);temporary=None
        return str(target)
    finally:
        if temporary:
            try:os.unlink(temporary)
            except OSError:pass
