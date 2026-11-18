"""Local failure IDs and complete tracebacks without dumping business snapshots."""
import logging
import uuid


def task_failure(exc,stage='准备操作'):
    """Keep established business messages; make empty/resource errors useful."""
    try:
        raw=str(exc)
        if raw.strip() and not isinstance(exc,MemoryError):return raw
        from work_failures import exception_text
        message='失败阶段：'+str(stage)+'\n原因：'+exception_text(exc)
        from application_runtime import current
        runtime=current()
        if runtime is not None:message+='\n诊断记录：'+str(runtime.folder/'app.log')
        return message
    except Exception:return '本次操作发生异常，未能取得完整原因；请查看本机应用日志。'


def failure(exc, *, stage, store='', sku='', source='', row=''):
    code=uuid.uuid4().hex[:12]
    context=f'阶段={stage} 门店={store} 商品={sku} 文件={source} 行={row}'
    logging.error('诊断 %s %s',code,context,exc_info=(type(exc),exc,exc.__traceback__))
    if isinstance(exc,KeyError) and exc.args:reason=f'缺少必需字段 {exc.args[0]}'
    elif isinstance(exc,MemoryError) or not str(exc).strip():
        from work_failures import exception_text
        reason=exception_text(exc)
    else:reason=str(exc)
    return f'{stage}失败；{reason}。诊断号 {code}。'+(f'门店 {store}；' if store else '')+(f'商品 {sku}；' if sku else '')+'请核对输入，重试仍失败时提供诊断号和本机日志。'
