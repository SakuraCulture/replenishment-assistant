"""Startup connection checks. No exports, task replay, or catalogue adoption."""
import time


def result(state, message, code=''):
    return {'state': state, 'message': message, 'code': code}


def yield_startup(host, kind, *, progress=lambda _: None, cancel=None):
    """Business work can cancel our read-only probe, never another business task."""
    startup = getattr(host, 'browser_startup', None)
    worker = startup.workers.get(kind) if startup else None
    if worker is None:
        return
    worker.cancel.handoff = True
    worker.cancel.set()
    progress('等待自动登录检查安全让出连接；原业务任务优先…')
    while not worker.released.wait(.1):
        if cancel and cancel.is_set():
            from runtime_support import Cancelled
            raise Cancelled('已取消等待连接')


def error_result(error):
    code = error.get('code', '') if isinstance(error, dict) else getattr(error, 'code', '')
    message = error.get('message', '') if isinstance(error, dict) else str(error)
    if code in {'LOGIN_REQUIRED', 'LOGIN_TIMEOUT', 'NOT_LOGGED_IN', 'WAIT_LOGIN'}:
        return result('LOGIN_REQUIRED', '请在对应浏览器完成登录；程序会自动复核。' + message, code)
    if code in {'WRONG_ACCOUNT', 'WRONG_IDENTITY'}:
        return result('WRONG_ACCOUNT', '当前登录账号与原绑定不同，请切回原账号后重新检查。' + message, code)
    if code in {'NOT_CONFIGURED', 'SETUP_REQUIRED', 'MACHINE_REBIND_REQUIRED', 'COMPANY_REQUIRED'}:
        return result('CONFIG_REQUIRED', message, code)
    return result('FAILED', message or '连接检查未完成，请查看详情后重试。', code)


def check_ax(client, cancel, report):
    name = '翱象'
    report(name, result('CHECKING', '正在复用原浏览器配置，连接并核验商家登录…'))
    client.start()
    if cancel.is_set():
        return
    config = client.request('configuration')
    if not config.get('configured') or not config.get('enabled', True):
        report(name, result('CONFIG_REQUIRED', '尚未保存翱象商家与门店绑定。请在“翱象配置”完成首次业务绑定；已保存后每次启动自动连接。', 'NOT_CONFIGURED'))
        return
    job = client.request('setup.check_login')
    ident = job['id']; stopped = False; deadline = time.monotonic() + 180
    while job['state'] == 'RUNNING' or job.get('cleanup_pending'):
        if ((cancel.is_set() and not getattr(cancel, 'handoff', False)) or time.monotonic() > deadline) and not stopped:
            client.request('setup.cancel', setup_id=ident); stopped = True
        if cancel.is_set() and getattr(cancel, 'handoff', False):
            report(name, result('CHECKING', '正在完成当前只读登录检查并交接原连接；不会另开浏览器连接。'))
        report(name, result('CHECKING', '正在核验登录：' + job.get('stage', '连接浏览器')))
        time.sleep(.15)
        job = client.request('setup.status', setup_id=ident, timeout=5)
    if cancel.is_set():
        return
    if job['state'] == 'VERIFIED' and not stopped:
        account = (job.get('identity') or {}).get('display_name', '')
        report(name, result('READY', '商家登录已核验' + ('：' + account if account else '') + '；共享连接可供原业务复用。'))
    else:
        report(name, error_result(job.get('error') or {'code': 'CHECK_TIMEOUT', 'message': '连接检查已停止或超时，请检查浏览器授权和网络。'}))


def check_jst(client, names, cancel, report):
    client.start()
    for name in names:
        if cancel.is_set():
            return
        state = client.request('sync.status')
        if state.get('running'):
            raise RuntimeError('原聚水潭操作尚未结束，稍后重新检查连接；未启动采集。')
        account = next((a for a in state.get('accounts', []) if a['warehouse'] == name), None)
        if account and not account.get('enabled'):
            report(name, result('DISABLED', '本仓自动同步已停用，保留原设置；需要时在仓配置中启用。'))
            continue
        report(name, result('CHECKING', '正在复用本仓专用浏览器并核验账号…' if account else '正在自动准备本仓独立浏览器，登录后自动核验保存…'))
        ack = client.request('accounts.ensure', warehouse=name, expected_version=account['bindingVersion'] if account else 0)
        ident = ack['operation_id']; stopped = False; deadline = time.monotonic() + 150
        while True:
            state = client.request('sync.status')
            op = state.get('operations', {}).get(ident)
            if not op:
                raise RuntimeError('账号核验记录缺失，请检查原连接；未重新提交。')
            if op['state'] != 'RUNNING' and (state.get('active') or {}).get('id') != ident:
                break
            if (cancel.is_set() or time.monotonic() > deadline) and not stopped:
                # Only cancel the read-only account operation that this check started.
                if (state.get('active') or {}).get('id') == ident:
                    client.request('sync.stop', batch_id=ident)
                stopped = True
            phase = (state.get('active') or {}).get('phase', {}).get('message')
            if phase:
                report(name, result('CHECKING', phase))
            time.sleep(.15)
        if cancel.is_set():
            return
        if op['state'] == 'SUCCEEDED' and not stopped:
            company = op.get('result', {}).get('scope', {}).get('company', '')
            report(name, result('READY', '本仓账号登录已核验' + ('：' + company if company else '') + '；未刷新仓资料。'))
        else:
            report(name, error_result(op.get('error') or {'code': 'CHECK_TIMEOUT', 'message': '本仓连接检查已停止或超时。'}))
