"""Independent file checking and bounded, ordered spawn scheduling. No Qt/config writes."""
from __future__ import annotations
import copy,multiprocessing
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor,wait,FIRST_COMPLETED
import engine
from runtime_support import checkpoint,Cancelled


def check_file(index,path):
    """Top-level spawn target. Only original sequence number and resolved path cross in."""
    report={'path':path,'rows':0,'stores':0,'error':''}
    try:
        before=engine.adapter.digest(Path(path))
        cfg=engine.config();sf=cfg['sales_fields']
        sheet,heads,rows,info=engine.read_identified(path,'sales',[sf[k] for k in ['store_code','sku','daily14','sales7','sales30','stock','inbound','stockout_days']])
        if before!=engine.adapter.digest(Path(path)):raise ValueError('读取期间文件变化，请保存后重试。')
        ambiguous=set(sf.values()) & set(info['duplicate_unused_headers'])
        if ambiguous:raise ValueError('已映射表头重复：'+ '、'.join(sorted(ambiguous)))
        staged=[];names={}
        for raw in rows:
            code,error=engine.adapter.identifier(raw.get(sf['store_code']))
            if error:raise ValueError(f"第{raw['_row']}行门店编码无效：{error}；请使用文本编码")
            sku,sku_error=engine.adapter.identifier(raw.get(sf['sku']))
            raw=copy.deepcopy(raw)
            raw['基础单位']=raw.get('基础单位',raw.get('库存单位',raw.get('单位类型')))
            values={k:v for k,v in raw.items() if k!='_row'}
            # Invalid SKU rows remain distinct and are diagnosed by the original adapter.
            key=(code,sku if not sku_error else f"待核:{path}:{raw['_row']}")
            staged.append((key,{'values':values,'path':path,'sheet':sheet,'row':raw['_row']}))
            names.setdefault(code,str(raw.get(sf['store_name']) or code))
        if not staged:raise ValueError('文件没有商品记录。')
        report.update(rows=len(staged),stores=len(names))
        return {'index':index,'report':report,'staged':staged,'names':names,'headers':sorted(heads|{'基础单位'}),'hash':before}
    except Cancelled:raise
    except Exception as exc:
        report['error']=str(exc)
        return {'index':index,'report':report}


def checked_files(paths,progress=lambda s:None,cancel=None,*,task=check_file):
    """At most two unmerged tasks. Completion order never controls merge order.

    Cancellation stops submission, cancels unstarted futures, discards results and
    joins running read-only tasks. No unsafe thread/process termination is used.
    The keyword-only task seam is for deterministic spawn tests, not UI settings.
    """
    if len(paths)==1:
        checkpoint(cancel);progress('读取补货建议：'+Path(paths[0]).name)
        result=task(0,paths[0]);checkpoint(cancel);yield result;return
    checkpoint(cancel)
    pool=ProcessPoolExecutor(max_workers=2,mp_context=multiprocessing.get_context('spawn'))
    pending={};ready={};submit=0;merge=0
    try:
        while merge<len(paths):
            checkpoint(cancel)
            while submit<len(paths) and len(pending)+len(ready)<2:
                checkpoint(cancel);progress('读取补货建议：'+Path(paths[submit]).name)
                future=pool.submit(task,submit,paths[submit]);pending[future]=submit;submit+=1
            done,_=wait(pending,timeout=.05,return_when=FIRST_COMPLETED)
            checkpoint(cancel)
            for future in done:
                index=pending.pop(future)
                # Unexpected process/pickle failures abort the batch, never a success report.
                result=future.result()
                if result['index']!=index:raise RuntimeError('文件检查进程返回序号错误')
                ready[index]=result
            while merge in ready:
                checkpoint(cancel);result=ready.pop(merge);merge+=1
                yield result
                del result
    finally:
        for future in pending:future.cancel()
        pool.shutdown(wait=True,cancel_futures=True)
