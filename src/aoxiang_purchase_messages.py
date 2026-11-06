"""Business explanations for purchase recovery; raw evidence stays available."""


def observation(record):
    return {k: record.get(k) for k in ('state', 'taskId', 'uploadAttempted', 'additionalTaskIds')} | {
        'orderId': (record.get('binding') or {}).get('orderId')}


def waiting(item, kind):
    return (kind == 'upload' and item['state'] in ('FAILED', 'REVIEW')
            and (item.get('error') or {}).get('code') == 'TASK_PENDING')


def explain(error, known=None):
    error, known = error or {}, known or {}
    code = error.get('code', '')
    messages = {
        'TASK_PENDING': '翱象已受理，采购单或核对文件仍在处理中。稍后点击“继续核对采购结果”，沿原任务检查完成状态。',
        'TASK_NOT_FOUND': '本次暂未查到原平台任务，已有任务号仍保留。请稍后继续核对采购结果；不要重新导入。',
        'IMPORT_TASK_FAILED': '翱象报告原导入任务未成功。请在平台原导入记录查看失败明细，并核对是否已有部分商品建单；不要整单重新导入。',
        'IMPORT_TASK_PARTIAL': '原导入任务未全部成功，可能已有部分商品建单。请核对平台原任务的成功及失败商品，再处理差异；不要整单重新导入。',
        'MULTIPLE_IMPORT_TASKS': '同一笔采购发现多个受理任务号，无法自动确定对应采购单。请到单据详情核对原采购记录，保留已有平台单据。',
        'LOGIN_REQUIRED': '翱象登录已失效。请在原浏览器重新登录并核验连接，再继续核对原任务。',
        'WRONG_IDENTITY': '当前翱象账号或商家与这笔采购不一致。请切回原商家并核验连接，再继续原任务。',
    }
    if code == 'SUBMISSION_UNKNOWN':
        if known.get('taskId'):
            return '已找到原受理任务号，之前的结果提示尚未更新。请点击“继续核对采购结果”，读取原任务并关联已有采购单。'
        if known.get('uploadAttempted'):
            return '上传已发起，但未收到可核验的受理回执，平台可能已建单。请在单据详情打开翱象采购，核对已有采购单；不要重新导入。'
        return '此记录尚未完成上传结果核对。请先点击“继续核对采购结果”重新读取原任务；若仍缺少回执，再到单据详情核对已有采购单。'
    return messages.get(code) or error.get('message') or '当前没有异常记录。'


def item_message(item, kind):
    if kind != 'upload':
        return (item.get('error') or {}).get('message', '')
    if item['state'] == 'BOUND':
        return '采购单已核验并关联；可在平台处理后续提交。'
    if item.get('error'):
        return explain(item['error'], item.get('purchase_observation'))
    if item.get('purchase_observation', {}).get('taskId'):
        return '已取得原受理任务号，正在核对采购结果。'
    return ''
