"""Host orchestration and recoverable adoption; no browser selectors or model rules."""
from __future__ import annotations
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid
import catalog_sync_validation as validation
import warehouse_catalog as wc
import work_repository as wr
from runtime_support import checkpoint, Cancelled, CommitProgress
from catalog_write_lock import acquire


class SyncProgress(str):
    def __new__(cls,message,data):
        obj=super().__new__(cls,message);obj.business_event=data;return obj


def stock_observation(dbpath,warehouse):
    import jst_orders as jst
    with closing(sqlite3.connect(dbpath)) as db:
        db.execute("BEGIN")
        state=jst.state(db,warehouse)
        included=[r[0] for r in db.execute('SELECT gid FROM jst_locks WHERE warehouse=? AND locked=1 ORDER BY rowid',(warehouse,))] if jst.exists(db) else []
    return {'warehouse':warehouse,'state':list(state) if state else None,'included':included,'observed_at':wr.now()}


class Adoptions:
    def __init__(self,folder,dbpath,state_root):
        self.folder=Path(folder);self.path=Path(dbpath);self.state_root=Path(state_root).resolve();self.catalogs=wc.Repository(folder)
    def connect(self):
        return sqlite3.connect(self.path,timeout=5)
    def ensure(self,progress=lambda _:None):
        import storage_chunks
        storage_chunks.backup_before_write(self.path,progress)
        with closing(self.connect()) as db,db:
            db.execute('CREATE TABLE IF NOT EXISTS catalog_sync_applies(id TEXT PRIMARY KEY,batch_id TEXT NOT NULL,item_id TEXT NOT NULL,warehouse TEXT NOT NULL,purpose TEXT NOT NULL,state TEXT NOT NULL,old_digest TEXT,new_digest TEXT,source_sha TEXT NOT NULL,payload TEXT NOT NULL,updated TEXT NOT NULL)')
    def get(self,ident):
        with closing(self.connect()) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='catalog_sync_applies'").fetchone():return None
            db.row_factory=sqlite3.Row;r=db.execute('SELECT * FROM catalog_sync_applies WHERE id=?',(ident,)).fetchone()
            return dict(r) if r else None
    def mark(self,ident,state):
        with closing(self.connect()) as db,db:db.execute('UPDATE catalog_sync_applies SET state=?,updated=? WHERE id=?',(state,wr.now(),ident))
    def recover(self,progress=lambda _:None,cancel=None):
        with closing(self.connect()) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='catalog_sync_applies'").fetchone():return []
            db.row_factory=sqlite3.Row;rows=[dict(r) for r in db.execute("SELECT * FROM catalog_sync_applies WHERE state='APPLYING' AND purpose='default_catalog'")]
        results=[]
        for row in rows:
            with acquire(self.folder,row['warehouse'],cancel):
                current=self.catalogs.current_digest(row['warehouse'])
                state='APPLIED' if current==row['new_digest'] else 'READY' if current==row['old_digest'] else 'CONFLICT'
                self.mark(row['id'],state);results.append({'id':row['id'],'state':state,'warehouse':row['warehouse']})
        return results
    def apply(self,batch,item,progress=lambda _:None,cancel=None):
        checkpoint(cancel)
        receipt=item.get('receipt');account=item['account'];warehouse=account['warehouse'];native=item.get('native') or {}
        if not receipt or receipt['warehouse']!=warehouse or native.get('sha256')!=receipt['source_sha256']:
            raise ValueError('下载、校验与仓绑定不一致')
        if (native.get('scope',{}).get('coid'),native.get('scope',{}).get('uid'))!=(account['expectedCoid'],account['expectedUid']):
            raise ValueError('原任务账号与仓绑定不符')
        root=(self.state_root/'downloads'/batch['id']/item['id']).resolve()
        for name in ('source','candidate'):
            if not Path(receipt[name]).resolve().is_relative_to(root):raise ValueError('采集文件不在原任务目录，未采用')
        ident=hashlib.sha256((batch['id']+':'+item['id']+':'+batch['purpose']+':'+receipt['source_sha256']).encode()).hexdigest()
        prior=self.get(ident)
        if prior and prior['state']=='APPLIED':return json.loads(prior['payload'])['ack']
        data=validation.candidate(receipt);self.ensure(progress);checkpoint(cancel)
        if batch['purpose']=='allocation_baseline':return self._stock(ident,batch,item,data,progress,cancel)
        if batch['purpose']!='default_catalog':raise ValueError('未知资料采用用途')
        from source_archive import capture
        capture(self.path,data['source_path'],data['source_sha256'],progress,cancel)
        expected=item['expected_catalog_digest']
        raw=json.dumps(data,ensure_ascii=False,indent=2).replace('\n',os.linesep).encode('utf-8');new_digest=hashlib.sha256(raw).hexdigest()
        ack={'apply_id':ident,'warehouse':warehouse,'source_sha256':data['source_sha256'],'revision':data['revision'],'purpose':'default_catalog','adopted_at':wr.now()}
        with acquire(self.folder,warehouse,cancel):
            prior=self.get(ident);current=self.catalogs.current_digest(warehouse)
            if prior and prior['state']=='APPLIED':return json.loads(prior['payload'])['ack']
            if prior and prior['state']=='APPLYING' and current==prior['new_digest']:
                self.mark(ident,'APPLIED');return json.loads(prior['payload'])['ack']
            if current!=expected:raise wr.Conflict('采集期间'+warehouse+'资料已被其他窗口更新，文件已留存但未采用')
            if prior and (prior['new_digest']!=new_digest or prior['old_digest']!=expected):raise wr.Conflict('原采用意图与当前校验候选不一致，请核对原记录')
            old=self.catalogs.catalogs().get(warehouse)
            if old and old.get('source_sha256')==data['source_sha256']:
                # Keep existing source_path/imported_at and exact default bytes.
                ack.update(revision=old['revision'],unchanged=True);new_digest=current
            payload={'ack':ack,'receipt':receipt,'account':account,'native_task_id':native.get('nativeTaskRef')}
            with closing(self.connect()) as db,db:
                checkpoint(cancel)
                db.execute('INSERT OR IGNORE INTO catalog_sync_applies VALUES(?,?,?,?,?,?,?,?,?,?,?)',(ident,batch['id'],item['id'],warehouse,batch['purpose'],'APPLYING',expected,new_digest,data['source_sha256'],json.dumps(payload,ensure_ascii=False),wr.now()))
            checkpoint(cancel);progress(warehouse+'正在保存资料；原件已留存')
            # Cancellation before replace is checked inside the same repository path.
            if not ack.get('unchanged'):
                progress(CommitProgress(True))
                try:self.catalogs.save_locked(data,expected_digest=expected,cancel=cancel)
                finally:progress(CommitProgress(False))
            self.mark(ident,'APPLIED')
        return ack
    def _stock(self,ident,batch,item,data,progress,cancel):
        import jst_orders as jst
        obs=item.get('observation')
        if not obs or obs.get('warehouse')!=data['warehouse']:raise ValueError('缺少原查询前的分配依据，未重置库存')
        prior=self.get(ident)
        if prior:
            payload=wr.loads(prior['payload']);plan=payload['plan'];ack=payload['ack']
        else:
            plan=jst.prepare_refresh(self.path,data['warehouse'],catalog=data,operation=ident,progress=progress,cancel=cancel)
            p=plan['payload']
            if (list(p['expected']) if p['expected'] else None)!=obs['state'] or p['included']!=obs['included']:
                raise wr.Conflict('采集期间锁单、分配或库存已变化，未采用新库存；请重新获取')
            ack={'apply_id':ident,'warehouse':data['warehouse'],'source_sha256':data['source_sha256'],'revision':data['revision'],'purpose':'allocation_baseline','bid':ident,'adopted_at':wr.now()}
            payload={'ack':ack,'plan':plan,'receipt':item['receipt']}
            with closing(self.connect()) as db,db:
                checkpoint(cancel)
                db.execute('INSERT INTO catalog_sync_applies VALUES(?,?,?,?,?,?,?,?,?,?,?)',(ident,batch['id'],item['id'],data['warehouse'],batch['purpose'],'APPLYING',None,None,data['source_sha256'],wr.dumps(payload),wr.now()))
        jst.commit_refresh(self.path,plan,progress,cancel);self.mark(ident,'APPLIED');return ack


def run_sync(client,folder,dbpath,warehouses,purpose='default_catalog',resume=None,progress=lambda _:None,cancel=None):
    """One worker coordinates Node, pre-query observation and local adoption."""
    adoptions=Adoptions(folder,dbpath,client.folder);adoptions.recover(progress,cancel)
    if not client.running:client.start()
    if resume:
        result=client.request('sync.resume',batch_id=resume['batch_id'],item_ids=resume['item_ids']);bid=result['batch_id']
    else:
        items=[{'warehouse':wh,'expected_catalog_digest':adoptions.catalogs.current_digest(wh)} for wh in warehouses]
        result=client.request('sync.start',operation_id=uuid.uuid4().hex,purpose=purpose,items=items);bid=result['batch_id']
    sent_stop=False;adoption_errors={};observations=set();last=None
    while True:
        if cancel is not None and cancel.is_set() and not sent_stop:
            client.request('sync.stop',batch_id=bid);sent_stop=True
        state=client.request('sync.status',batch_id=bid);batch=state['batches'][0]
        for item in batch['items']:
            if item['stage']=='WAIT_HOST' and not item.get('observation') and not sent_stop:
                token=item['observationRequest']
                if token not in observations:
                    obs=stock_observation(dbpath,item['account']['warehouse']);client.request('sync.provide_observation',batch_id=bid,item_id=item['id'],token=token,observation=obs);observations.add(token)
            if item.get('receipt') and not item.get('adoption') and item['id'] not in adoption_errors and not sent_stop:
                try:
                    ack=adoptions.apply(batch,item,progress,cancel)
                    client.request('sync.acknowledge_adoption',batch_id=bid,item_id=item['id'],receipt=ack)
                    catalog=adoptions.catalogs.catalogs().get(ack['warehouse']) if ack['purpose']=='default_catalog' else None
                    progress(SyncProgress(ack['warehouse']+'资料已采用',{'kind':'adopted','ack':ack,'catalog':catalog}))
                except Cancelled:
                    sent_stop=True;client.request('sync.stop',batch_id=bid)
                except Exception as exc:
                    adoption_errors[item['id']]=str(exc)
                    client.request('sync.record_adoption_error',batch_id=bid,item_id=item['id'],message=str(exc))
        signature=json.dumps({'batch':batch,'active':state['active'],'errors':adoption_errors},ensure_ascii=False,sort_keys=True)
        if signature!=last:
            progress(SyncProgress('正在处理仓商品资料' if state['running'] else '本轮采集结束',{'kind':'status','state':state,'adoption_errors':dict(adoption_errors)}));last=signature
        if not state['running']:
            final=client.request('sync.status',batch_id=bid)['batches'][0]
            return {'batch':final,'adoption_errors':adoption_errors,'stopped':sent_stop,'adopted':sum(bool(i.get('adoption')) for i in final['items'])}
        time.sleep(.15)
