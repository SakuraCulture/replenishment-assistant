"""Business explanations for purchase recovery; raw evidence stays available."""

PAYMENT_PREPARATION_CODES = frozenset({'PAYMENT_MISSING', 'PAYMENT_PENDING', 'PAYMENT_BASIS_CHANGED','PAYMENT_CANCEL_REVIEW',
                                       'PAYMENT_UPSTREAM_NOT_READY', 'PAYMENT_SELECTION_CHANGED', 'PAYMENT_ZERO_QUANTITY'})


def observation(record):
    return {k: record.get(k) for k in ('state', 'taskId', 'uploadAttempted', 'additionalTaskIds')} | {
        'orderId': (record.get('binding') or {}).get('orderId'), 'candidateOrderId': record.get('candidateOrderId'),
        'confirmedImportRequests': record.get('confirmedImportRequests')} | {
        k: record[k] for k in ('uploadWarning', 'importResult') if record.get(k)}


def import_failure(record):
    record = record or {}
    result = record.get('importResult') or {}
    if result.get('taskId') and result['taskId'] == record.get('taskId') and result.get('kind') in ('failed','partial'):
        return result
    return None


def import_failure_message(result):
    label = '导入失败' if result['kind']=='failed' else '部分成功'
    success, failure = (result.get(k) for k in ('successCount','failCount'))
    counts = f"成功 {success if success is not None else '未留存'}、失败 {failure if failure is not None else '未留存'}"
    detail = '失败原件已留存。' if result.get('failureFile') else '失败原件暂未取得，可在翱象原任务查看。'
    return f'翱象原任务{label}（{counts}）。{detail}原任务号 {result["taskId"]}。'


def waiting(item, kind):
    return (kind == 'upload' and item['state'] in ('FAILED', 'REVIEW')
            and (item.get('error') or {}).get('code') == 'TASK_PENDING')


def voided(item):
    return item['state']=='VOIDED' or (item.get('platform_observation') or {}).get('status')=='已作废' or (item['state'] == 'BOUND' and ((item.get('result') or {}).get('binding') or {}).get('status') == '已作废')


def explain(error, known=None):
    error, known = error or {}, known or {}
    code = error.get('code', '')
    messages = {
        'PAYMENT_MISSING': '尚无已确认付款单。需要采购时，请先在生命周期导入并核对付款单。',
        'PAYMENT_PENDING': '付款单尚未确认。需要采购时，请先在生命周期完成付款核对。',
        'PAYMENT_BASIS_CHANGED': '付款单的对照依据已变化。需要采购时，请先在生命周期重新核对付款单。',
        'PAYMENT_UPSTREAM_NOT_READY': '付款单的前置业务资料尚未就绪。需要采购时，请先在生命周期核对前置资料和付款单。',
        'IDENTITY_RESPONSE_TIMEOUT': '原任务仍保留；本次核对账号身份时未取得响应，尚不能判断登录失效或采购失败。请确认原浏览器页面与网络可用，再继续核对原任务；不会重传。',
        'IDENTITY_CHECK_FAILED': '本次未完成账号身份核验，尚不能判断登录失效。请检查原浏览器页面及连接后继续原任务，不要重新上传。',
        'PURCHASE_STORE_NOT_FOUND': '原任务仍保留；采购页暂未定位到指定门店，尚未完成查单。请核对门店目录的编码、名称及页面加载后继续核对采购结果，不要重新上传。',
        'IMPORT_SIDE_EFFECT_BLOCKED': '上传保护已阻断额外请求。已有唯一受理回执时只核对原任务和采购明细；尚无回执时保留待核，不能重新上传。具体被拦请求见技术详情。',
        'TASK_PENDING': '翱象已受理，采购单或核对文件仍在处理中。稍后点击“继续核对采购结果”，沿原任务检查完成状态。',
        'IMPORT_TASK_WAIT_EXPIRED': '自动核对窗口已结束，原受理号保留。点击“继续核对采购结果”可查询同一任务。',
        'TASK_NOT_FOUND': '本次暂未查到原平台任务，已有任务号仍保留。请稍后继续核对采购结果；不要重新导入。',
        'IMPORT_TASK_FAILED': '翱象报告原导入任务未成功，请继续核实原任务及采购单结果。',
        'IMPORT_TASK_PARTIAL': '原导入任务未全部成功，请沿原任务核对成功、失败商品及已有采购单。',
        'PURCHASE_ORIGINAL_UNRESOLVED': '请继续核对原采购。已有受理、采购和实收待办均按原单处理。',
        'MULTIPLE_IMPORT_TASKS': '同一笔采购发现多个受理任务号，无法自动确定对应采购单。请到单据详情核对原采购记录，保留已有平台单据。',
        'LOGIN_REQUIRED': '翱象登录已失效。请在原浏览器重新登录并核验连接，再继续核对原任务。',
        'WRONG_IDENTITY': '当前Edge登录已变化，与这笔原采购的身份不同；连接无需重新配置。原采购不会自动改绑或重传，请核对原采购归属和当前登录权限后继续原任务。',
        'PURCHASE_SUPERSEDED': '旧采购任务已结束并重建；请打开后继任务。旧平台单未自动撤销，原回执仍保留。',
        'PURCHASE_STATUS_UNSUPPORTED': '原采购单已找到，但当前状态不在自动核对范围。请在技术详情查看原始状态，继续核对原单。',
        'PURCHASE_STATUS_CHANGED': '平台查询状态与核对文件不同，暂不关联。请稍后继续核对采购结果，保留原任务和已导出文件；不要重新导入。',
    }
    if code in PAYMENT_PREPARATION_CODES:
        # Keep the order/stage/version explanation supplied by local validation.
        return error.get('message') or messages[code]
    details = error.get('details') or {}
    if result := import_failure(known):
        message=import_failure_message(result)
        if code and code != result.get('errorCode'):
            message+=' 最近一次核对：'+explain(error, {k: v for k,v in known.items() if k!='importResult'})
        return message
    if details.get('stage') and code in ('TASK_NOT_FOUND','PURCHASE_VERIFY_TIMEOUT'):
        return error.get('message') or details['stage']+'未完成，保留原任务。'
    source = details.get('sourceId')
    if known.get('taskId') and source in ('D16', 'D17') and code in ('SUBMISSION_UNKNOWN', 'PURCHASE_VERIFY_EXPORT_UNKNOWN'):
        if source == 'D16' and not details.get('taskId') and details.get('canRecoverMain', True) and not known.get('additionalTaskIds'):
            return ('原采购导入已受理，采购主单核对导出缺少回执。点击“继续核对采购结果”，只处理原采购单的核对资料。'
                    '已有导出回执时沿用原导出任务，缺回执时重新导出核对主单，采购单不会重复上传。')
        label = '采购主单' if source == 'D16' else '采购商品明细'
        return '原采购导入受理号已保留。' + label + '核对导出的受理情况待核，请核对该导出的原任务记录。'
    if code == 'SUBMISSION_UNKNOWN':
        if known.get('taskId'):
            return '已找到原受理任务号。点击“继续核对采购结果”，只读取原采购任务并核对已有采购单。'
        if known.get('uploadAttempted'):
            return '上传已发起，可能已建单。请从单据详情或原批次继续核对原采购。'
        return '此记录尚未完成上传结果核对。请先点击“继续核对采购结果”重新读取原任务；若仍缺少回执，再到单据详情核对已有采购单。'
    if code == 'BROWSER_OPERATION_FAILED' and ('next-tree-node-label' in str(error.get('message')) or 'storeCodeList' in str(error.get('message'))):
        return messages['PURCHASE_STORE_NOT_FOUND']
    return messages.get(code) or error.get('message') or '当前没有异常记录。'


def item_message(item, kind):
    if kind=='upload' and (item.get('purchase_observation') or {}).get('state')=='MANUAL_HANDOFF':
        return '本单人工处理待核。请选择本行打开“人工处理”，导出原件或核对已有采购。'
    if item.get('rebuild_to'):return '旧任务已转入重建批次 '+item['rebuild_to']+'；旧平台单未撤销，原记录保留。'
    if kind=='upload' and item['state']!='BOUND' and import_failure(item.get('purchase_observation')):
        return explain(item.get('error'),item['purchase_observation'])
    if item['state']=='ACCEPTED':return '平台已受理并正在处理；等待时可提交其他订单，不重复上传本单。'
    if item['state']=='VOIDED':return '平台已作废，常规同步与最终实收待办已停止；原签发、付款和平台关联保留，未记收货0。'
    if kind != 'upload':
        if kind == 'prepare' and (item.get('error') or {}).get('code') == 'PURCHASE_ALREADY_RESERVED':
            return '已有原采购任务，本次没有重复上传。可继续原任务；需要重做时，勾选本单后点击“结束所选任务并重建预览”，旧采购记录保留。'
        if kind == 'prepare' and (item.get('error') or {}).get('code') in PAYMENT_PREPARATION_CODES:
            return explain(item['error'])
        return (item.get('error') or {}).get('message', '')
    if item['state'] == 'BOUND':
        if item.get('error'):
            return '已有关联保留。本次核对未完成，'+explain(item['error'], item.get('purchase_observation'))
        if voided(item):
            return '已核实原采购单及全部商品明细，平台状态为已作废。可返回删除页重新核对完整范围；本次没有重传或操作平台作废。'
        warning=(item.get('purchase_observation') or {}).get('uploadWarning') or (item.get('result') or {}).get('uploadWarning')
        return '采购单已核验并关联；此处保留上次核验结果，平台当前状态以平台为准。'+(' 上传时曾阻断额外请求，随后沿原任务核验通过；记录保留在技术详情。' if warning else '')
    if item.get('error'):
        return explain(item['error'], item.get('purchase_observation'))
    if item.get('purchase_observation', {}).get('taskId'):
        return '已取得原受理任务号，正在核对采购结果。'
    return ''
