"""Local Qt gateway and task coordinator. The UI remains the workspace owner."""
import copy
import json
import logging
import secrets
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from PySide6.QtCore import QObject, Signal, QThread, QLockFile, QTimer
from PySide6.QtNetwork import QLocalServer
from PySide6.QtWidgets import QApplication
from automation_protocol import VERSION, MAX_FRAME, CommandError, digest, decode, validate_call, wire_default
from automation_tasks import Journal
import automation_jobs


def database_busy(exc):
    return isinstance(exc, sqlite3.OperationalError) and (getattr(exc, 'sqlite_errorcode', 0) & 255) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)


class Job(QThread):
    progress = Signal(str)
    store_event = Signal(str, str, object, str)
    result_ready = Signal(object)
    error_ready = Signal(str, str)

    def __init__(self, fn, parent):
        super().__init__(parent); self.fn = fn; self.cancel = threading.Event()

    def run(self):
        try: self.result_ready.emit(self.fn(self))
        except Exception as exc:
            from runtime_support import Cancelled
            if isinstance(exc, Cancelled): self.error_ready.emit('CANCELLED', '已停止准备，原作业保持不变')
            else:
                logging.exception('AI 任务失败')
                self.error_ready.emit('TASK_FAILED', str(exc))


class Gateway(QObject):
    changed = Signal()

    def __init__(self, host):
        super().__init__(host)
        self.host = host; self.instance = uuid.uuid4().hex
        self.enabled = False; self.server = None; self.lock = None; self.journal = None
        self.secret = ''; self.buffers = {}; self.prepared = {}; self.calculations = {}
        self.last_client = None
        self.registration_error = None
        self.active = None; self.worker = None; self.last_stage = 0; self.explanations = {}
        self.task_failures = {}; self.pending_failures = set()
        self.failure_timer = QTimer(self); self.failure_timer.setInterval(1000)
        self.failure_timer.timeout.connect(self.persist_failures)
        self.folder = Path(host.work_repository.path).parent / 'automation'
        self.connection_file = self.folder / 'connection.json'
        self.workspace = digest(str(Path(host.work_repository.path).resolve()))[:24]

    def enable(self):
        if self.enabled: return
        self.folder.mkdir(parents=True, exist_ok=True)
        lock = QLockFile(str(self.folder / 'owner.lock')); lock.setStaleLockTime(0)
        if not lock.tryLock(0): raise CommandError('BUSY', '另一补货助手窗口已启用 AI 连接，请在该窗口使用或关闭连接')
        server = QLocalServer(self); server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        name = 'replenishment-' + self.workspace + '-' + self.instance
        if not server.listen(name):
            lock.unlock(); server.deleteLater(); raise CommandError('CONNECTION_FAILED', '无法建立本机连接：' + server.errorString())
        try:
            journal = Journal(self.host.work_repository.path, self.instance)
            self.secret = secrets.token_urlsafe(32)
            from priority_list import atomic_json
            atomic_json(self.connection_file, {'protocol': VERSION, 'server': name, 'token': self.secret,
                'instance': self.instance, 'workspace': self.workspace, 'created': time.time()})
            from automation_connection import refresh_registration
            try:
                refresh_registration(self);self.registration_error=None
            except (OSError,ValueError):
                # Optional client registration must remain repairable from this window.
                self.registration_error='本机登记更新未完成，请点击配置/修复本机连接；当前工作台连接仍可使用。'
                logging.warning(self.registration_error)
        except Exception:
            server.close(); server.deleteLater(); lock.unlock(); raise
        self.server, self.lock, self.journal = server, lock, journal
        self.server.newConnection.connect(self.accept_connections)
        self.enabled = True; self.changed.emit()
        if self.pending_failures: self.failure_timer.start()

    def disable(self):
        if self.worker:
            self.stop_remaining({'task_id': self.active})
            raise CommandError('STOPPING', '正在停止后续任务，请待当前步骤安全结束后关闭连接')
        if self.server:
            self.server.close()
            for socket in list(self.buffers): socket.abort()
            self.server.deleteLater(); self.server = None
        if self.connection_file.exists():
            try:
                if json.loads(self.connection_file.read_text('utf-8'))['instance'] == self.instance:
                    self.connection_file.unlink()
            except (OSError, ValueError, KeyError): pass
        if self.lock: self.lock.unlock(); self.lock = None
        self.failure_timer.stop()
        self.stockout_reports={}
        if hasattr(self,'stockout_expiry'):self.stockout_expiry.stop()
        self.enabled = False; self.secret = ''; self.changed.emit()
        self.last_client = None

    def accept_connections(self):
        while self.server and self.server.hasPendingConnections():
            s = self.server.nextPendingConnection()
            if len(self.buffers) >= 8: s.abort(); s.deleteLater(); continue
            self.buffers[s] = bytearray()
            s.readyRead.connect(lambda socket=s: self.read_connection(socket))
            s.disconnected.connect(lambda socket=s: self.drop(socket))

    def drop(self, socket):
        if socket not in self.buffers:return
        self.buffers.pop(socket, None)
        from shiboken6 import isValid
        if isValid(socket):socket.deleteLater()

    def read_connection(self, socket):
        if socket not in self.buffers: return
        buffer = self.buffers[socket]; buffer.extend(bytes(socket.readAll()))
        if len(buffer) > MAX_FRAME: socket.abort(); return
        while b'\n' in buffer:
            line, _, remaining = buffer.partition(b'\n'); buffer[:] = remaining
            request = {}
            try:
                request = decode(line)
                if not isinstance(request, dict) or request.get('protocol') != VERSION or not secrets.compare_digest(str(request.get('token', '')), self.secret):
                    raise CommandError('UNAUTHORIZED', '本机连接许可无效，请重新启用连接')
                if not self.enabled or self.host.closing: raise CommandError('CLOSED', '工作台正在关闭或连接已停用')
                from version_info import VERSION as APP_VERSION
                if request.get('workspace') and request['workspace'] != self.workspace:
                    raise CommandError('WORKSPACE_MISMATCH','资料环境不一致，未切换工作台，请重新配置连接')
                if request.get('bridge_version') and request['bridge_version'] != APP_VERSION and request.get('action') != 'workbench_context':
                    raise CommandError('VERSION_MISMATCH','接口程序与工作台版本不同，请在客户端重新加载补货助手连接')
                if QApplication.activeModalWidget() is not None and request.get('action') not in {'workbench_context', 'task_status', 'stop_remaining'}:
                    raise CommandError('UI_WAITING', '工作台正在等待业务处理弹窗，请先完成或取消该操作')
                value = self.dispatch(request.get('action'), request.get('arguments', {}))
                if request.get('action')=='workbench_context':
                    self.last_client={'name':str(request.get('client_name','旧版接口'))[:120], 'at':time.time(),
                        'version_match':request.get('bridge_version')==APP_VERSION,'instance':self.instance}
                    self.changed.emit()
                response = {'id': request.get('id'), 'ok': True, 'result': value}
            except CommandError as exc:
                response = {'id': request.get('id') if isinstance(request, dict) else None, 'ok': False,
                            'error': {'code': exc.code, 'message': str(exc), 'details': exc.details}}
            except Exception as exc:
                logging.exception('AI 命令处理失败')
                response = {'id': request.get('id') if isinstance(request, dict) else None, 'ok': False,
                            'error': {'code': 'BUSY' if database_busy(exc) else 'INTERNAL_ERROR',
                                      'message': '数据正在被其他操作使用，请稍后查询原任务状态，不要重复提交。' if database_busy(exc) else '操作未完成；已保留本地诊断，请核对原任务状态后再继续'}}
            raw = json.dumps(response, ensure_ascii=False, allow_nan=False, default=wire_default).encode() + b'\n'
            if len(raw) > MAX_FRAME:
                raw = json.dumps({'id': request.get('id'), 'ok': False, 'error': {'code': 'TOO_LARGE', 'message': '结果过大，请缩小范围或分页读取'}}).encode() + b'\n'
            socket.write(raw); socket.flush()

    def dispatch(self, action, arguments):
        validate_call(action, arguments)
        return getattr(self, action)(arguments)

    def token(self):
        h = self.host
        # Browsing/filtering also calls touch(). Bind to business state, not that UI signal.
        return digest([self.instance, h.job_day, id(h.loaded), h.input_dirty, h.readonly,
            h.sales.paths, h.products.path, h.master_legacy.isChecked(), h.profile,
            {s: h.current_parameter_record(s) for s in h.store_widgets}, h.active_plan,
            {s: [id(r), r.get('preissue'), r.get('new_products')] for s, r in h.results.items()},
            getattr(h, '_mapping_error', '')])

    def check_context(self, token):
        if token != self.token(): raise CommandError('STALE_CONTEXT', '当前作业、参数或结果已变化，请重新检查；旧结果没有覆盖工作台')

    def idle(self):
        entry=getattr(self.host,'stockout',None)
        if entry and (getattr(entry,'worker',None) or getattr(entry,'queued',None)):
            raise CommandError('BUSY','门店缺货率正在采集或维护，请等待安全结束后分析')
        dialog=getattr(self.host,'_large_io_dialog',None)
        if dialog is not None and not getattr(dialog,'complete',getattr(dialog,'_completed',False)):raise CommandError('BUSY','当前有后台业务任务，等待完成后再操作')
        if self.pending_failures: raise CommandError('BUSY', 'AI 任务状态正在等待保存，请稍后查询原任务；不会重复计算或采用结果')
        scheduled = getattr(self.host, 'scheduled', None)
        if self.worker or self.host.worker or (scheduled and (scheduled.worker or scheduled.publisher)):
            raise CommandError('BUSY', '当前已有任务运行，请查看进度或等待当前步骤结束再操作')
        if self.host.closing: raise CommandError('CLOSED', '工作台正在关闭')

    def workbench_context(self, args):
        from version_info import VERSION as APP_VERSION, MODEL_VERSION, BUILD
        h = self.host
        scheduled = getattr(h, 'scheduled', None)
        return {'instance_id': self.instance, 'workspace_id': self.workspace, 'context_token': self.token(),
            'business_date': h.job_day, 'application': APP_VERSION, 'model': MODEL_VERSION,
            'protocol': VERSION, 'build': {k: BUILD.get(k) for k in ('source_commit', 'build_id')},
            'scheduled_access': 'read_only', 'checked_input_present': bool(h.loaded) and not h.input_dirty,
            'readonly': h.readonly, 'input_dirty': h.input_dirty, 'unsaved_changes': h.job_dirty,
            'selected_stores': h.selected_codes(), 'active_task': self.active,
            'busy': bool(h.worker or (scheduled and (scheduled.worker or scheduled.publisher))), 'enabled': self.enabled, 'scope': '查询、已有文件检查、预案计算和试算；定时任务只读；不触发平台取数、不保存默认、不签发、不采购、不删除',
            'summary': [{'store': s, 'name': v['name'], 'state': v['state']} for s, v in h.states.items()]}

    def find_resources(self, args):
        import replenishment_plans as plans
        import automation_queries as queries
        h = self.host; kind = args['kind']; offset = args.get('offset', 0); limit = args.get('limit', 50)
        if kind in ('schedules', 'scheduled_runs'): return queries.scheduled_resources(h.work_repository.path, args)
        if kind == 'stores':
            known = plans.available(h); rows = [{'code': c, 'name': n} for c, n in known.items()]
        elif kind == 'plans': rows = [{**{k: p[k] for k in ('id', 'name', 'revision', 'stores')},
            'parameter_mode':p.get('parameter_mode','legacy_fields'),
            'parameter_fields': p.get('parameter_fields'), 'note': ('逐店选择完整参数预案；stores.preset_id为空表示跟随门店默认，准备时冻结。预算和主从仓独立。' if p.get('parameter_mode')=='per_store_presets' else '旧预案：parameter_fields为空列表表示不覆盖参数；null为旧版完整预案。实际覆盖值在准备预览中核对。')} for p in plans.Repository(h.work_repository.path).list()]
        elif kind == 'files': rows = queries.file_resources(h)
        elif kind == 'catalogs': rows = [{**{k: c.get(k) for k in ('warehouse', 'revision', 'source_path', 'source_sha256')},
            'note': '已登记的仓资料；不代表本次输入已完成匹配或可计算。'} for c in h.catalogs.values()]
        elif kind == 'orders': rows = h.orders.headers()
        else:
            value = self.journal.page(offset, limit, args.get('search', ''))
            value['items'] = [self.task_failures.get(r['task_id'], r) for r in value['items']]
            return value
        search = args.get('search', '').casefold()
        if search: rows = [r for r in rows if search in json.dumps(r, ensure_ascii=False).casefold()]
        return {'total': len(rows), 'offset': offset, 'items': rows[offset:offset + limit], 'next_offset': offset + limit if offset + limit < len(rows) else None}

    def effective_parameters(self, args):
        from automation_queries import parameter_result
        preview = None
        if args.get('scope') == 'preview':
            if not args.get('preview_id'): raise CommandError('INVALID_ARGUMENT', '查询试算预览须提供 preview_id')
            preview = self.require_prepared(args['preview_id'])
        elif args.get('preview_id'): raise CommandError('INVALID_ARGUMENT', 'preview_id 只能用于 scope=preview')
        return {'context_token': self.token(), **parameter_result(self.host, args, preview)}

    def parameter_definitions(self, args):
        from parameter_guidance import catalog
        return catalog(args)

    def query_order_funnel(self, args):
        from automation_analytics import query
        return query(self,args)

    def query_inventory_structure(self,args):
        from automation_inventory import query
        return query(self,args)

    def stockout_context(self,args):
        from automation_stockout import context
        return context(self,args)

    def query_stockout_analysis(self,args):
        from automation_stockout import query
        return query(self,args)

    def stockout_analysis_results(self,args):
        from automation_stockout import results
        return results(self,args)

    def stockout_filter_values(self,args):
        from automation_stockout import results
        return results(self,args,'filters')

    def stockout_evidence(self,args):
        from automation_stockout import results
        return results(self,args,'evidence')

    def export_stockout_analysis(self,args):
        from automation_stockout import export
        return export(self,args)

    def inventory_structure_results(self,args):
        from automation_inventory import results
        return results(self,args)

    def export_inventory_structure(self,args):
        from automation_inventory import export
        return export(self,args)

    def order_funnel_results(self, args):
        from automation_analytics import results
        return results(self,args)

    def export_order_funnel(self, args):
        from automation_analytics import export
        return export(self,args)

    def store_history_review(self, args):
        from automation_analytics import require
        from automation_queries import page
        report=require(self,args['task_id'])
        review=report['history']
        rows=review['items']
        if args.get('store'):rows=[r for r in rows if r['store']==args['store']]
        return {k:v for k,v in review.items() if k!='items'}|{'page':page(rows,args.get('offset',0),args.get('limit',50))}

    def scheduled_run_status(self, args):
        from automation_queries import scheduled_status
        return scheduled_status(self.host.work_repository.path, args)

    def start_stock_diagnosis(self,args):
        from automation_diagnosis import start
        return start(self,args)

    def stock_diagnosis_results(self,args):
        from automation_diagnosis import results
        return results(self,args)

    def stock_diagnosis_parameters(self,args):
        from automation_diagnosis import parameters
        return parameters(self,args)

    def adopt_stock_diagnosis(self,args):
        from automation_diagnosis import adopt
        return adopt(self,args)

    def undo_stock_diagnosis(self,args):
        from automation_diagnosis import undo
        return undo(self,args)

    def snapshot(self, arguments):
        import parameter_maintenance as pm
        import replenishment_plans as plans
        h = self.host; plan = None
        from parameter_presets import freeze,snapshot as parameter_snapshot
        parameter_data=parameter_snapshot(h.work_repository)
        if arguments.get('plan_id'):
            plan = next((p for p in parameter_data['plans'] if p['id'] == arguments['plan_id']), None)
            if plan is None: raise CommandError('PLAN_NOT_FOUND', '所选预案已不存在，请重新查询')
            plan=freeze(plan,h.work_repository,data=parameter_data)
        return {'loaded': h.loaded, 'input_dirty': h.input_dirty, 'selected': h.selected_codes(),
            'current_records': {s: h.current_parameter_record(s) for s in h.store_widgets},
            'origins': copy.deepcopy(h._store_parameter_origins), 'previous_defaults': copy.deepcopy(getattr(h, 'saved_store_defaults', {})),
            'defaults': parameter_data['stores'], 'budgets': h.repository.budgets(), 'routes': h.catalog_repository.routes(),
            'mapping': pm.mapping_snapshot(h.work_repository.path), 'catalogs': h.catalogs,
            'legacy': h.master_legacy.isChecked(), 'products': h.products.path,
            'profile': h.profile, 'plan': plan, 'active_plan': copy.deepcopy(h.active_plan), 'workers': h.concurrency.value(),
            'context_token': self.token(), 'dbpath': str(h.work_repository.path), 'day': h.job_day}

    def task_record(self, task_id):
        return copy.deepcopy(self.task_failures[task_id]) if task_id in self.task_failures else self.journal.get(task_id)

    def fail_callback(self, task, exc):
        """Fail closed if callback persistence fails. Never replay a business callback."""
        tid = task['task_id']
        if tid in self.task_failures: return
        logging.exception('AI 任务结果处理未完成：%s', tid)
        if self.worker: self.worker.cancel.set()
        self.prepared.pop(tid, None)
        message = ('数据被其他操作占用，任务状态等待保存；本任务未完成，请勿重复提交。'
                   if database_busy(exc) else '结果处理未完成，已保留诊断；请核对原任务。')
        self.task_failures[tid] = {**task, 'state':'failed', 'message':message,
                                  'error_code':'TASK_STATUS_WRITE_FAILED' if database_busy(exc) else 'TASK_HANDLER_FAILED',
                                  'status_persistence_pending':True}
        self.pending_failures.add(tid); self.failure_timer.start()
        self.host.status.setText('AI 协助 · '+message)
        self.changed.emit()

    def persist_failures(self):
        # Retry only the rolled-back metadata transaction, never calculation,
        # source retention, adoption, signing or a platform request. No UI wait.
        for tid in list(self.pending_failures):
            value = self.task_failures[tid]
            try:
                saved = self.journal.update(tid, _timeout=0, state='failed', error_code=value['error_code'],
                    status_persistence_pending=False, message='任务未完成；状态已保存，请核对后重新检查。')
            except Exception as exc:
                if database_busy(exc): continue
                logging.exception('AI 失败状态保存仍未完成：%s', tid)
                self.pending_failures.discard(tid)
                value['message']='任务未完成，状态无法保存；请查看本地诊断，原任务不要重复提交。'
            else:
                self.task_failures[tid]=saved; self.pending_failures.discard(tid)
        if not self.pending_failures: self.failure_timer.stop()
        self.changed.emit()

    def start_job(self, task, fn, finish, store_handler=None):
        h = self.host; worker = Job(fn, h); self.worker = worker; self.active = task['task_id']; h.worker = worker
        self.last_stage = 0; h.update_controls()
        def progress(message):
            if task['task_id'] in self.task_failures: return
            h.status.setText('AI 协助 · ' + message)
            if time.monotonic() - self.last_stage >= 1:
                try:
                    self.last_stage = time.monotonic(); self.journal.update(task['task_id'], message=message); self.changed.emit()
                except Exception as exc: self.fail_callback(task, exc)
        worker.progress.connect(progress)
        if store_handler:
            def store_event(*args):
                if task['task_id'] in self.task_failures: return
                try: store_handler(*args)
                except Exception as exc: self.fail_callback(task, exc)
            worker.store_event.connect(store_event)
        def completed(value):
            if task['task_id'] in self.task_failures: return
            try: finish(value)
            except Exception as exc: self.fail_callback(task, exc)
        worker.result_ready.connect(completed)
        def failed(code, message):
            if task['task_id'] in self.task_failures: return
            try:
                self.journal.update(task['task_id'], state='stopped' if code == 'CANCELLED' else 'failed', message=message, error_code=code)
                self.changed.emit()
            except Exception as exc: self.fail_callback(task, exc)
        worker.error_ready.connect(failed)
        def done():
            if h.worker is worker: h.worker = None
            self.worker = None; self.active = None; worker.deleteLater(); h.update_controls(); self.changed.emit()
            if h.closing: h.close()
        worker.finished.connect(done); self.changed.emit(); worker.start()

    def prepare_calculation(self, args):
        existing=self.journal.lookup('prepare:' + args['operation_key'],args)
        if existing:return self.task_record(existing['task_id'])
        self.idle(); self.check_context(args['context_token'])
        if self.host.readonly and args['mode'] == 'current': raise CommandError('READONLY', '历史作业只读，请先新建或恢复今日作业')
        task, created = self.journal.create('prepare:' + args['operation_key'], args, 'prepare')
        if not created: return task
        try: frozen = self.snapshot(args)
        except Exception:
            self.journal.update(task['task_id'], state='failed', message='准备失败，请核对当前配置'); raise
        def finish(p):
            if self.worker.cancel.is_set():
                self.journal.update(task['task_id'], state='stopped', message='准备已停止，未采用结果');return
            self.prepared[task['task_id']] = p
            while len(self.prepared)>5:
                self.prepared.pop(next(iter(self.prepared)))
            self.journal.update(task['task_id'], state='prepared' if p['report']['ready'] else 'needs_input',
                message='检查完成，可以计算' if p['report']['ready'] else '请处理门店配置或冲突', preview_id=task['task_id'], preview=p['report'])
            self.changed.emit()
        self.start_job(task, lambda w: automation_jobs.prepare(frozen, args, w.progress.emit, w.cancel), finish)
        return task

    def require_prepared(self, pid):
        if pid in self.task_failures: raise CommandError('NEEDS_REVIEW', '该检查未完成，请查询原任务状态后重新检查')
        p = self.prepared.get(pid)
        if p is None: raise CommandError('SNAPSHOT_UNAVAILABLE', '原预览快照不在当前会话，请重新检查')
        if not p['report']['ready']: raise CommandError('NEEDS_INPUT', '存在未解决的配置或冲突，请在工作台处理', p['report'])
        return p

    def run_calculation(self, args):
        pid = args['preview_id']; p = self.require_prepared(pid)
        # Duplicate calls return the original task even while the worker is busy.
        existing = self.calculations.get(pid)
        if existing: return self.journal.get(existing)
        self.idle(); self.check_context(p['context_token'])
        if p['mode'] == 'current': self.check_adoption(p)
        return self.begin_calculation(p, p['codes'], 'run:' + pid, {'preview_id': pid}, pid)

    def begin_calculation(self, p, codes, key, arguments, pid=None, parent=None):
        if parent is None:
            # Keep three recent root batches (including retries), never an unbounded session cache.
            roots=[tid for tid,r in self.calculations.items() if isinstance(r,dict) and not r.get('parent')]
            for root in roots[:-2]:
                removed={root}
                while True:
                    more={tid for tid,r in self.calculations.items() if isinstance(r,dict) and r.get('parent') in removed}
                    if more<=removed:break
                    removed.update(more)
                for tid in removed:
                    old=self.calculations.pop(tid)
                    for preview,value in list(self.prepared.items()):
                        if value is old['prepared']:self.prepared.pop(preview)
                    self.journal.update(tid,result_evicted=True,message='会话结果已释放（保留最近3批）；已采用的工作台结果、保存记录和签发不受影响')
                for alias,value in list(self.calculations.items()):
                    if isinstance(value,str) and value in removed:self.calculations.pop(alias)
        task, created = self.journal.create(key, arguments, 'calculate', parent_task=parent,
            stores=[{'code': s, 'name': p['loaded']['stores'][s], 'state': '排队中', 'error': ''} for s in codes])
        if not created: return task
        result = {'prepared': p, 'results': {}, 'states': {s: {'name': p['loaded']['stores'][s], 'state': '排队中', 'error': ''} for s in codes}, 'codes': codes, 'parent': parent}
        self.calculations[task['task_id']] = result
        if pid: self.calculations[pid] = task['task_id']
        def event(s, state, value, error):
            result['states'][s].update(state=state, error=error)
            if value is not None: result['results'][s] = value
            summary = [{'code': c, **v} for c, v in result['states'].items()]
            completed = sum(v['state'] in ('已完成', '计算失败', '已停止') for v in result['states'].values())
            self.journal.update(task['task_id'], stores=summary, progress={'completed': completed, 'total': len(codes)}, message=f'计算进度 {completed}/{len(codes)} 家')
            self.changed.emit()
        def finish(_):
            statuses = [s['state'] for s in result['states'].values()]
            state = 'completed' if all(s == '已完成' for s in statuses) else 'partial' if result['results'] else 'stopped' if all(s == '已停止' for s in statuses) else 'failed'
            self.journal.update(task['task_id'], state=state, message='计算结束；未保存、未签发', result_ref=task['task_id'])
            ancestor_id = parent
            while ancestor_id:
                ancestor=self.require_result(ancestor_id)
                ancestor['states'].update(copy.deepcopy(result['states']));ancestor['results'].update(result['results'])
                self.journal.update(ancestor_id, stores=[{'code':s,**ancestor['states'][s]} for s in ancestor['codes']],
                    state='completed' if all(x['state']=='已完成' for x in ancestor['states'].values()) else 'partial',
                    message='单店重试结果已并回原批次', last_retry=task['task_id'])
                ancestor_id = ancestor.get('parent')
            if p['mode'] == 'current' and result['results'] and not self.host.closing and not self.journal.get(task['task_id']).get('human_takeover'):
                try: self.apply_result(task['task_id'], internal=True)
                except Exception as exc:
                    self.journal.update(task['task_id'], state='needs_review', message='计算结果保留，但未采用：' + str(exc))
            self.changed.emit()
        def execute(w):
            if p['mode']=='current':automation_jobs.retain_sources(p,w.progress.emit,w.cancel)
            return automation_jobs.calculate(p, codes, w.store_event.emit, w.cancel)
        self.start_job(task, execute, finish, event)
        return task

    def task_status(self, args):
        p = self.task_record(args['task_id'])
        try: events = self.journal.events(args['task_id'], args.get('after', 0))
        except Exception as exc:
            if not database_busy(exc) or args['task_id'] not in self.task_failures: raise
            events = []  # Keep the cursor: no event was consumed while locked.
        if args['task_id'] in self.explanations:
            from history_read import repository
            from archive_orders import require_active
            from contextlib import closing
            value = self.explanations[args['task_id']]
            with closing(repository(self.host.work_repository.path).work.connect()) as db:
                require_active(db, value['order']['oid'], value['warehouse'])
            p['explanation'] = value
        return {**p, 'events': events, 'cursor': events[-1]['cursor'] if events else args.get('after', 0),
                'in_current_session': args['task_id'] in self.prepared or args['task_id'] in self.explanations or isinstance(self.calculations.get(args['task_id']), dict)}

    def stop_remaining(self, args):
        task = self.task_record(args['task_id'])
        if self.active == args['task_id'] and self.worker:
            self.worker.cancel.set()
            try: task = self.journal.update(args['task_id'], state='stopping', message='停止后续请求已收到；正在计算的门店完成后结束')
            except Exception as exc:
                self.fail_callback(task, exc); task = self.task_record(args['task_id'])
        self.changed.emit(); return task

    def require_result(self, tid):
        if tid in self.task_failures: raise CommandError('NEEDS_REVIEW', '该任务结果处理未完成，请先核对原任务状态')
        result = self.calculations.get(tid)
        if not isinstance(result, dict): raise CommandError('RESULT_UNAVAILABLE', '结果不在当前会话；已保存资料请到原草稿或订单入口查询')
        return result

    def retry_failed(self, args):
        existing=self.journal.lookup('retry:'+args['operation_key'],args)
        if existing:return self.task_record(existing['task_id'])
        self.idle(); r = self.require_result(args['task_id']); p = r['prepared']; self.check_context(p['context_token'])
        if not args['stores'] or any(s not in r['states'] or r['states'][s]['state'] not in ('计算失败', '已停止') for s in args['stores']):
            raise CommandError('NOT_RETRYABLE', '只能重试原任务中失败或已停止的门店')
        return self.begin_calculation(p, args['stores'], 'retry:' + args['operation_key'], args, parent=args['task_id'])

    def calculation_results(self, args):
        import result_semantics as sem
        r = self.require_result(args['task_id']); selected = args.get('store'); rows = []
        if selected is not None and selected not in r['codes']: raise CommandError('STORE_NOT_FOUND', '门店不在原任务中')
        summaries = [{'store': s, 'name': r['states'][s]['name'], 'state': r['states'][s]['state'],
                      'summary': r['results'].get(s, {}).get('summary')} for s in r['codes'] if selected is None or s == selected]
        if selected:
            for d in r['results'].get(selected, {}).get('rows', []):
                if args.get('warehouse') and d.get('AU') != args['warehouse']: continue
                if args.get('search', '').casefold() not in (str(d.get('D', '')) + str(d.get('E', ''))).casefold(): continue
                q = sem.order_quantity(d)
                if args.get('filter') == 'positive' and not (sem.numeric(q) and q > 0): continue
                if args.get('filter') == 'issues' and not (sem.issue_text(d) or sem.exclusion_reason(d) or sem.inbound_risk(d) or not sem.numeric(q)): continue
                rows.append({'sku': d.get('E'), 'name': d.get('D'), 'warehouse': d.get('AU'), 'quantity': q,
                    'amount': sem.order_amount(d), 'monthly_sales': d.get('J'), 'available': d.get('K'), 'in_transit': d.get('L'), 'reason': sem.explanation(d)})
        offset = args.get('offset', 0); limit = args.get('limit', 50)
        return {'stores': summaries, 'total': len(rows), 'offset': offset, 'items': rows[offset:offset + limit], 'next_offset': offset + limit if offset + limit < len(rows) else None,
                'note': '未选择门店时仅返回汇总；进一步按门店查询商品。'}

    def explain_decision(self, args):
        import business_presenter as bp
        if bool(args.get('task_id')) == bool(args.get('order_id')): raise CommandError('INVALID_ARGUMENT', '请指定任务或历史订单中的一种')
        if args.get('order_id'):
            self.idle()
            from archive_orders import number
            from app_defaults import WAREHOUSES
            heads=[]
            for h in self.host.orders.headers():
                if args.get('store') and h['store']!=args['store']:continue
                child=next((w for w in WAREHOUSES if number(h['number'],w)==args['order_id']),None)
                if args['order_id'] in (h['oid'],h['number']) or child:
                    if child and args.get('warehouse') and args['warehouse']!=child:raise CommandError('WAREHOUSE_MISMATCH','订单号与指定仓库不一致')
                    if child:args={**args,'warehouse':child}
                    heads.append(h)
            if len(heads)!=1:raise CommandError('ORDER_NOT_FOUND','请使用查询到的准确订单编号')
            head=heads[0]; key='history:'+digest([head,args])
            task,created=self.journal.create(key,{'head':head,'arguments':args},'explanation',order_ref=head['oid'])
            if created:
                def finish(value):
                    from deletion_barrier import require
                    from contextlib import closing
                    with closing(self.host.work_repository.connect()) as db:require(db,head['oid'])
                    self.explanations[task['task_id']] = value
                    self.journal.update(task['task_id'], state='completed', message='已读取当时记录；解释仅当前会话有效', order_ref=head['oid'])
                self.start_job(task,lambda w:automation_jobs.historical_explanation(self.host.work_repository.path,head,args,w.cancel),
                    finish)
            return task
        r = self.require_result(args['task_id']); hits = []
        for s in r['codes']:
            if args.get('store') and s != args['store']: continue
            for d in r['results'].get(s, {}).get('rows', []):
                if str(d.get('E')) == args['sku'] and (not args.get('warehouse') or d.get('AU') == args['warehouse']): hits.append((s, d))
        if len(hits) != 1: raise CommandError('AMBIGUOUS_PRODUCT', '请明确门店和仓；找到 ' + str(len(hits)) + ' 条对应商品')
        s, d = hits[0]; result = r['results'][s]
        return {'store':s,**automation_jobs.explain_row(result,d)}

    def check_adoption(self, p):
        from workbench_actions import recalculation_impact
        h = self.host
        if h.readonly: raise CommandError('READONLY', '历史作业只读')
        import deletion_barrier
        from contextlib import closing
        with closing(h.work_repository.connect()) as db:
            deletion_barrier.validate(db,{'day':p['day'],'generations':{s:p.get('deletion_generations',{}).get(s,0) for s in p['codes']}})
        impact = recalculation_impact(h.results, p['codes'])
        if impact['new_products'] or impact['manual']:
            raise CommandError('NEEDS_REVIEW', '重算涉及已有新品选择或人工调整，请先在原工作台核对后重新准备',
                {'new_products': impact['new_products'], 'manual': list(impact['manual']), 'view': 'results'})
        if p['paths'] and h.loaded is not None and h.loaded is not p['loaded']:
            raise CommandError('REPLACE_INPUT_REVIEW', '使用新文件替换已有作业，请先在准备数据页采用文件；可以继续试算但不覆盖现有结果')

    def adopt_trial(self, args):
        self.idle(); self.check_context(args['context_token'])
        r=self.require_result(args['task_id']);p=r['prepared']
        if p['paths']:
            self.check_context(p['context_token']);self.check_adoption(p)
            task,created=self.journal.create('adopt:'+args['task_id'],args,'adopt')
            if created:
                def execute(w):automation_jobs.retain_sources(p,w.progress.emit,w.cancel)
                def finish(_):
                    if self.worker.cancel.is_set():self.journal.update(task['task_id'],state='stopped',message='采用已停止，原件留存可能已完成');return
                    value=self.apply_result(args['task_id'],internal=True)
                    self.journal.update(task['task_id'],state='completed',message=value['message'])
                self.start_job(task,execute,finish)
            return task
        return self.apply_result(args['task_id'])

    def apply_result(self, tid, internal=False):
        r = self.require_result(tid); p = r['prepared']; h = self.host
        self.check_context(p['context_token']); self.check_adoption(p)
        if not r['results']: raise CommandError('NO_RESULTS', '没有可采用的已完成结果')
        from workbench_actions import record_settings
        import admission
        for s in r['results']:
            if admission.snapshot(h.work_repository.path, s) != p['jobs'][s]['admission']:
                raise CommandError('ADMISSION_CHANGED', '门店准入资料已变化，请重新检查：' + s)
        previous_worker = h.worker; h.worker = None
        try:
            if p['paths'] and h.loaded is not p['loaded']:
                h.sales.paths = list(p['paths']); h.sales.refresh()
                if hasattr(h, 'auto_check'): h.auto_check.stop()
                h.loaded_ok(p['loaded'])
            h.initializing = True
            try:
                h.__dict__.setdefault('deletion_generations',{}).update(p.get('deletion_generations',{}))
                for s in r['codes']:
                    record = p['records'][s]
                    h.store_presets[s] = copy.deepcopy(record['preset']); h.overrides[s] = copy.deepcopy(record['preset']['config']['warehouses'])
                    if s in p['explicit_parameters']:h._store_parameter_origins.pop(s,None)
                    h.set_store_ui(s, {**record['ui'], 'selected': True}); h.refresh_parameter_label(s)
                    h.states[s] = copy.deepcopy(r['states'][s])
                    if s in r['results']: h.results[s] = r['results'][s]
                    else: h.results.pop(s, None)
                h.active_plan = copy.deepcopy(p['plan'] or p.get('active_plan')); h.run_codes = list(dict.fromkeys([*h.run_codes, *r['codes']]))
                h.reset_calculation_snapshot()
                h._calculation_input = h.loaded; h._calculation_profile = copy.deepcopy(p['profile']); h._calculation_settings = copy.deepcopy(p['jobs'])
                h.current_store = None; h.result = None; h.dirty = True
            finally: h.initializing = False
            h.selection_changed(); h.refresh_tables(); h.touch(); h.navigate(3)
            self.journal.update(tid, adopted=True, message='结果已采用到当前作业；尚未保存或签发')
            p['context_token'] = self.token()
        finally: h.worker = previous_worker
        return {'task_id': tid, 'adopted': True, 'context_token': self.token(), 'message': '已采用，尚未保存或签发'}

    def open_workbench_view(self, args):
        h = self.host; view = args['view']
        if view in ('home', 'work'): h.show_section(0 if view == 'home' else 1)
        elif view in ('progress', 'results'):
            h.show_section(1); h.navigate(2 if view == 'progress' else 3)
            if args.get('store'):
                if args['store'] not in h.states: raise CommandError('STORE_NOT_FOUND', '门店不在当前作业')
                h.current_store=args['store']; h.result=h.results.get(args['store']); h.dirty=True
                h.navigate(4)
        elif view == 'tracking': h.show_section(3)
        elif view == 'parameters': h.show_section(4)
        else: h.show_section(2)
        return {'opened': view, 'message': '已打开原业务页面，未执行保存或签发'}
