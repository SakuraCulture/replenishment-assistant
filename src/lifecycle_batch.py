"""Read/preview only batch orchestration. All quantity rules stay in the service.

Whole source orders are never summed across files. A duplicate target/stage must
be resolved by selecting one candidate before preview. File order is retained;
within each target only the necessary lifecycle dependency order is applied.
"""
import copy
from pathlib import Path
import lifecycle_io as io
import order_lifecycle as service
import work_repository as wr
from runtime_support import checkpoint
from export_progress import RowProgress


def automatic_specs(info, stage=None):
    """Only unambiguous headers; no filename/date/SKU similarity inference."""
    specs=[];unresolved=[]
    paid={s['sheet']:s for s in io.payment_specs(info)}
    for sheet in info['sheets']:
        choices=[]
        for s in ([stage] if stage else io.STAGES):
            if s=='payment' and sheet['sheet'] in paid:
                choices.append((s,paid[sheet['sheet']]))
                continue
            mapping=io.auto_mapping(s,sheet['headers'])
            if all(mapping.get(f) is not None for f in io.required(s) if f!='数量单位'):
                choices.append((s,{**info,**sheet,'mapping':mapping,'defaults':{'数量单位':'库存单位'}}))
        if len(choices)==1:
            s,spec=choices[0];specs.append((s,{**spec,'order_number_only':True}))
        else:unresolved.append(sheet['sheet'])
    return specs,unresolved


def group_scan(scanned):
    groups={}
    for row in scanned['rows']:
        source=row.get('source_order')
        identity=([row.get('signed_number'),*[source.get(k) for k in ('内部订单号','线上订单号','店铺名称','买家账号','平台站点')]]
                  if source else [row.get(k) for k in ('signed_number','store','warehouse','document_number','document_date')])
        groups.setdefault(wr.dumps(identity),[]).append(row)
    return [{'scan':{**scanned,'rows':rows},'target':None,'selected':True} for rows in groups.values()]


def read(paths, stage=None, progress=lambda _:None, cancel=None):
    """Return successes and explicit unresolved files; caller must resolve/exclude."""
    documents=[];issues=[];files=[];seen=set()
    for i,path in enumerate(dict.fromkeys(str(Path(p).resolve()) for p in paths)):
        checkpoint(cancel);progress(RowProgress(f'识别单据 {i+1}/{len(paths)}：{Path(path).name}',i,len(paths)))
        try:
            info=io.inspect([path],progress,cancel)[0]
            if info['sha'] in seen:
                files.append({'name':info['name'],'status':'内容完全重复，未重复读取','sheets':[]});continue
            seen.add(info['sha']);specs,unknown=automatic_specs(info,stage)
            # Unknown sheets are not silently discarded, including empty sheets.
            if unknown:issues.append({'info':info,'stage':stage,'message':'工作表未明确识别：'+'、'.join(unknown)})
            scans={}
            for s,spec in specs:scans.setdefault(s,[]).append(spec)
            pending=[]
            for s,ss in scans.items():pending.extend(group_scan(io.scan(s,ss,progress,cancel)))
            documents.extend(pending)
            files.append({'name':info['name'],'status':'已识别' if not unknown else '需核对读取范围','sheets':[(s,p['sheet']) for s,p in specs]})
        except Exception as exc:
            from runtime_support import Cancelled
            if isinstance(exc,Cancelled):raise
            issues.append({'path':path,'stage':stage,'message':str(exc)})
    return {'documents':documents,'issues':issues,'files':files}


def prepare(path, documents, scope=None, progress=lambda _:None, cancel=None):
    selected=[copy.deepcopy(d) for d in documents if d.get('selected',True)]
    if not selected:raise ValueError('尚未选择要保存的完整单据')
    grouped={};seen=set()
    allowed={h['number'] for h in scope} if scope is not None else None
    for d in selected:
        checkpoint(cancel);scan=d['scan'];target=d.get('target')
        numbers={r.get('signed_number') or (target or {}).get('number') for r in scan['rows']}
        if len(numbers)!=1 or not next(iter(numbers)):raise ValueError('请为缺号的完整来源订单选择补货单号')
        number=next(iter(numbers))
        if target and number!=target['number']:raise ValueError('文件单号与所选订单不一致')
        if allowed is not None and number not in allowed:raise ValueError('资料超出勾选订单范围：'+number)
        identity=(number,scan['stage'])
        if identity in seen:raise ValueError(number+' 的'+io.TITLES[scan['stage']]+'有多份候选，请只勾选一份完整单据；不会拼单或累加')
        seen.add(identity);grouped.setdefault(number,[]).append(d)
    plans=[];files={};staged={};count=len(selected)
    for group in grouped.values():
        for d in sorted(group,key=lambda x:io.STAGES.index(x['scan']['stage'])):
            checkpoint(cancel)
            result=service.prepare_numbered(path,d['scan'],d.get('target'),progress,cancel,staged=staged)
            if len(result['plans'])!=1:raise ValueError('来源单据未按完整目标订单分组')
            p=result['plans'][0]
            # Validate an explicitly selected scope against its frozen snapshot too.
            if scope is not None:
                h=next(h for h in scope if h['number']==p['head']['number'])
                if (h['revision'],h['digest'])!=(p['head']['revision'],p['head']['digest']):raise wr.Conflict('所选订单已重新签发，请刷新后核对')
            plans.append(p);staged[(*service.key(p['head']),p['stage'])]=service.staged_document(p)
            files.update({f['sha']:f for f in result['files']})
            progress(RowProgress(f'核对单据 {len(plans)}/{count}',len(plans),count))
    return {'plans':plans,'files':list(files.values())}
