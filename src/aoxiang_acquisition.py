"""Ordered immutable replenishment acquisition manifests; no calculation or platform selectors."""
import hashlib
import re
import uuid
from pathlib import Path

from aoxiang_directory import atomic_json, file_hash
from aoxiang_jobs import encoded, now

ACTIVE = {'QUEUED', 'RUNNING', 'PAUSE_REQUESTED'}
RESUMABLE = {'PAUSED', 'INTERRUPTED'}


def prepare(jobs, configuration, codes, freshness, *, day):
    if not configuration.get('configured') or not configuration.get('enabled'):
        raise ValueError('请先核验并保存翱象连接与门店')
    if freshness not in {'reuse', 'refresh'}:
        raise ValueError('取数方式无效')
    available = {item['buyerWarehouseCode']: item for item in configuration['targets']}
    if not codes or len(codes) > 256 or len(set(codes)) != len(codes):
        raise ValueError('请选择1至256家门店，不能重复')
    if any(not isinstance(code, str) or code not in available for code in codes):
        raise ValueError('所选门店不在已核验绑定中，请重新核验')
    for pending in jobs.unfinished():
        old = pending['command']
        if (old['operation'] == 'replenishment.export' and old['account_binding'] == configuration['account_binding']
                and set(codes).intersection(old['scope']['warehouse_codes'])):
            raise ValueError('所选门店还有未完成或待核对的原采集，不能另建任务。请继续原采集，或先“结束本次采集”再新建；已受理平台任务将保留接续：' + pending['id'])
    command = {'protocol_version': 1, 'command_id': 'rep-' + uuid.uuid4().hex,
               'operation': 'replenishment.export', 'account_binding': configuration['account_binding'],
               'scope': {'warehouse_codes': list(codes)},
               'basis_versions': {'store_mapping': configuration['store_mapping_version']},
               'policy': {'freshness': freshness}}
    context = {'day': day, 'created': now(), 'stores': [available[code] for code in codes]}
    return jobs.register(command, context)


def manifest(job, state_root):
    """Accept only complete validated files in the command's original order, even after interruption."""
    record, command = job.get('record'), job['command']
    if not record or record['state'] in ACTIVE:
        raise ValueError('请等待完成或安全暂停，再采用已验收文件')
    if record.get('id') != job['id'] or record.get('request') != command:
        raise ValueError('作业回执关联不符')
    codes = command['scope']['warehouse_codes']
    items = record['items']
    if len(items) != len(codes):
        raise ValueError('门店检查结果不完整')
    files, omitted = [], []
    for index, (code, item) in enumerate(zip(codes, items)):
        if type(item['index']) is not int or item['index'] != index or item['warehouse_code'] != code:
            raise ValueError('门店顺序或编码变化，未采用')
        if item['state'] != 'SUCCEEDED':
            omitted.append({'index': index, 'warehouse_code': code, 'state': item['state'], 'error': item.get('error')})
            continue
        result = item['result']
        if (result['warehouse_code'] != code or result['run_id'] != item['run_id'] or
                result['account_binding'] != command['account_binding']):
            raise ValueError('文件身份、门店或原作业关联不符')
        digest = result['sha256']
        if not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
            raise ValueError('文件校验值无效')
        expected = Path(state_root).resolve() / 'command-files' / job['id'] / f'{index}-{digest}.xlsx'
        actual = Path(result['file']).resolve()
        if actual != expected.resolve() or not actual.is_file():
            raise ValueError('命令原件位置不符或缺失，不能猜测同日文件')
        if actual.stat().st_size != result['bytes'] or file_hash(actual) != digest:
            raise ValueError('已验收文件变化，未采用')
        files.append(dict(result, index=index))
    if not files:
        raise ValueError('本次没有可采用的已验收文件')
    return {'schema': 1, 'command_id': job['id'], 'request': command,
            'context': job['context'], 'sequence': record['sequence'], 'files': files, 'omitted': omitted}


def freeze_manifest(value, folder):
    digest = hashlib.sha256(encoded(value).encode('utf-8')).hexdigest()
    target = Path(folder) / 'manifests' / (value['command_id'] + '-' + digest + '.json')
    if target.exists():
        import json
        if json.loads(target.read_text(encoding='utf-8')) != value:
            raise ValueError('原文件采用清单已变化')
    else:
        atomic_json(target, value)
    return str(target)


def verify_loaded(value, loaded):
    hashes = loaded['intake']['hashes']
    reports = loaded['intake']['files']
    expected = [item['file'] for item in value['files']]
    if [r['path'] for r in reports] != expected:
        raise ValueError('检查后的文件顺序变化，原工作区保留')
    for item, report in zip(value['files'], reports):
        if report['error'] or hashes.get(item['file']) != item['sha256']:
            raise ValueError('已下载文件未通过原检查或校验值变化，原工作区保留：' + (report['error'] or item['warehouse_code']))
    return loaded
