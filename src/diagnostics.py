"""Local failure IDs and complete tracebacks without dumping business snapshots."""
import logging
import uuid


def failure(exc, *, stage, store='', sku='', source='', row=''):
    code=uuid.uuid4().hex[:12]
    context=f'阶段={stage} 门店={store} 商品={sku} 文件={source} 行={row}'
    logging.error('诊断 %s %s',code,context,exc_info=(type(exc),exc,exc.__traceback__))
    reason=f'缺少必需字段 {exc.args[0]}' if isinstance(exc,KeyError) else str(exc)
    return f'{stage}失败；{reason}。诊断号 {code}。'+(f'门店 {store}；' if store else '')+(f'商品 {sku}；' if sku else '')+'请核对输入，重试仍失败时提供诊断号和本机日志。'
