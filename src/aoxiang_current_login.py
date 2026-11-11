"""Observe the selected Edge login before a new business operation; no exports."""
import time
from aoxiang_client import ComponentError
from runtime_support import Cancelled, checkpoint

def current_configuration(client,progress=lambda _:None,cancel=None):
    checkpoint(cancel)
    config=client.request('configuration')
    # Older embedded transports and metadata-only fixtures do not advertise this
    # capability. Never send an unrecognised command or infer a current identity.
    if not config.get('configured') or config.get('login_policy')!='current_edge':return config
    progress('正在读取当前Edge登录；不要求配置账号ID，也不创建平台任务…')
    began=time.monotonic();job=client.request('setup.check_login');deadline=began+180;stopped=False
    while job['state']=='RUNNING' or job.get('cleanup_pending'):
        if ((cancel is not None and cancel.is_set()) or time.monotonic()>deadline) and not stopped:
            client.request('setup.cancel',setup_id=job['id']);stopped=True
        progress((job.get('stage') or '核验当前Edge登录')+f' · 已用 {int(time.monotonic()-began)} 秒 · 尚未开始本次取数')
        time.sleep(.15);job=client.request('setup.status',setup_id=job['id'])
    if cancel is not None and cancel.is_set():raise Cancelled('已停止当前登录核验；未创建平台任务')
    if stopped or job['state']!='VERIFIED':
        e=job.get('error') or {}
        detail=e.get('details') or {};message=e.get('message','当前Edge登录尚未核验完成，请检查浏览器中的登录和连接许可')
        if detail.get('phase'):message+='\n核验环节：'+str(detail['phase'])
        raise ComponentError(e.get('code','LOGIN_CHECK_INCOMPLETE'),message,detail)
    config=client.request('configuration')
    if config.get('needs_store_verification'):
        raise ComponentError('TARGET_UNVERIFIED','当前Edge登录已接通：'+str(config.get('display_name') or '当前商家')+'。请在“翱象连接与门店配置”核验该商家的门店；无需填写平台账号ID。')
    return config


def current_template(client,receipt,config,progress=lambda _:None,cancel=None):
    """Keep a template only for the observed identity; never resubmit a purchase."""
    identity=(receipt or {}).get('identity') or {};binding=config.get('account_binding') or {}
    if receipt and identity.get('accountId')==binding.get('account_id') and identity.get('merchantCode')==binding.get('merchant_code'):
        return receipt
    checkpoint(cancel);progress('登录已变化，正在获取当前登录的采购模板；尚未创建采购单…')
    job=client.request('template.start');deadline=time.monotonic()+180;stopped=False
    while job.get('state')=='RUNNING' or job.get('cleanup_pending'):
        if ((cancel is not None and cancel.is_set()) or time.monotonic()>deadline) and not stopped:
            client.request('template.cancel',template_id=job['id']);stopped=True
        time.sleep(.15);job=client.request('template.status',template_id=job['id'])
        progress(job.get('stage') or '核验当前采购模板')
    checkpoint(cancel)
    if stopped or job.get('state')!='READY':
        error=job.get('error') or {}
        raise ComponentError(error.get('code','TEMPLATE_FAILED'),error.get('message','当前登录的采购模板未完成核验；未创建采购单'))
    return job['receipt']
