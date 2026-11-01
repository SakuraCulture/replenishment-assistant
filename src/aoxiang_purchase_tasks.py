"""Local purchase packages. Original payment and all replenishment tables are read-only here."""
import copy
import json
import os
import uuid
from decimal import Decimal
from pathlib import Path

from aoxiang_jobs import encoded
from aoxiang_directory import file_hash
from aoxiang_purchase import freeze_payment, validate_basis
from aoxiang_purchase_excel import generate
from runtime_support import checkpoint


def _write_new(path, value):
    with Path(path).open('x', encoding='utf-8', newline='\n') as stream:
        stream.write(encoded(value) + '\n')
        stream.flush()
        os.fsync(stream.fileno())


def prepare(path, head, configuration, suppliers, receipt, directory, contract_path, *, cancel=None, progress=lambda _: None):
    plan = freeze_payment(path, head, configuration, suppliers, cancel=cancel)
    command_id = 'purchase-' + uuid.uuid4().hex
    target = Path(directory) / command_id
    target.mkdir(parents=True, exist_ok=False)
    marker = 'AX_CREATE:' + command_id
    note = marker + ' 本地单:' + plan['head']['number']
    generated = generate(plan, receipt, contract_path, target / '采购上传.xlsx', cancel=cancel, progress=progress, note=note)
    checkpoint(cancel)
    # The component keeps a separate durable intent before any upload, not just this marker.
    intent = {'schema': 1, 'operation': 'purchase.create', 'id': command_id,
              'identity': {'accountId': plan['account_binding']['account_id'], 'merchantCode': plan['account_binding']['merchant_code']},
              'mappingVersion': plan['store_mapping_version'],
              'basis': {'number': plan['head']['number'], 'warehouse': plan['head']['warehouse'], 'store': plan['head']['store'],
                        'planFingerprint': plan['fingerprint'], 'paymentVersion': plan['payment']['version'], 'paymentDigest': plan['payment']['digest']},
              'target': copy.deepcopy(plan['platform_store']), 'supplierCode': plan['supplier_code'],
              'marker': marker, 'note': note, 'createExternalOrder': False, 'postCreationState': '待提交',
              'file': {'path': generated['file'], 'sha256': generated['sha256'], 'bytes': generated['bytes']},
              'template': {'recordFile': receipt['recordFile'], 'sha256': receipt['sha256'], 'attemptId': receipt['attemptId']},
              'rows': [{k: format(Decimal(r[k]), 'f') if k in ('quantity', 'amount') else r[k] for k in ('sku', 'name', 'quantity', 'amount')} for r in plan['rows']],
              'totalQuantity': format(Decimal(plan['total_quantity']), 'f'), 'totalAmount': format(Decimal(plan['total_amount']), 'f')}
    validate_basis(path, plan, configuration, suppliers, cancel=cancel)
    _write_new(target / '付款采购依据.json', plan)
    _write_new(target / '采购文件核验.json', generated)
    _write_new(target / '采购任务.json', intent)
    return {'id': command_id, 'package_file': str((target / '采购任务.json').resolve()),
            'package_sha256': file_hash(target / '采购任务.json'), 'plan_file': str((target / '付款采购依据.json').resolve()),
            'plan_sha256': file_hash(target / '付款采购依据.json'), 'file': generated, 'intent': intent}


def validate_for_upload(path, package, configuration, suppliers, *, cancel=None):
    """Called by the host immediately before dispatch. No automatic resends on timeout."""
    checkpoint(cancel)
    for field in ('package', 'plan'):
        if file_hash(package[field + '_file']) != package[field + '_sha256']:
            raise ValueError('采购冻结依据已发生变化，请核对，不能上传')
    intent = json.loads(Path(package['package_file']).read_text(encoding='utf-8'))
    plan = json.loads(Path(package['plan_file']).read_text(encoding='utf-8'))
    if intent['id'] != package['id'] or intent['basis']['planFingerprint'] != plan['fingerprint']:
        raise ValueError('采购任务和付款依据关联不符')
    if file_hash(intent['file']['path']) != intent['file']['sha256']:
        raise ValueError('采购上传文件已变化')
    validate_basis(path, plan, configuration, suppliers, cancel=cancel)
    return True


def inspect_basis(path, package, configuration, suppliers):
    """Read-only UI assessment. Dispatch/save still repeat their original checks.

    Check the frozen business basis, not the possibly removed upload workbook.
    Call from the existing background worker, once per selected record/refresh.
    """
    try:
        if not package or not Path(path).is_file():
            raise ValueError('原采购付款依据未找到，无法确认是否仍有效')
        plan_file = Path(package['plan_file'])
        if file_hash(plan_file) != package['plan_sha256']:
            raise ValueError('原采购付款依据文件已变化，不能作为有效依据')
        plan = json.loads(plan_file.read_text(encoding='utf-8'))
        validate_basis(path, plan, configuration, suppliers)
    except ValueError as exc:
        reason = str(exc)
    except (KeyError, TypeError, OSError):
        reason = '原采购付款依据缺失、字段不完整或无法读取'
    except Exception:
        import logging
        logging.exception('读取翱象采购依据状态失败')
        reason = '当前无法完成依据核对，请保留原记录并检查日志'
    else:
        return {'valid':True, 'message':'采购依据已核对：与当前已确认付款一致（本次打开或刷新时）。'}
    return {'valid':False, 'message':'采购依据待核：'+reason+'。原采购记录保留，不会自动改量或重建；请先核对变更。'}
