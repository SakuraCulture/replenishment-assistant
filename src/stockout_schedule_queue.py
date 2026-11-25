"""Durable trigger and handoff for the existing stockout hub worker."""
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from stockout_common import TZ, encoded

SOURCES=(('stockout-schedule.json','store'),('warehouse-stockout-schedule.json','warehouse'),('stockout-hub-schedule.json',None))


class Queue:
    def __init__(self,folder):
        self.folder=Path(folder)
        self.path=self.folder/'analysis'/'stockout_hub'/'scheduled.sqlite3'

    @contextmanager
    def connect(self,write=False):
        if write:self.path.parent.mkdir(parents=True,exist_ok=True)
        db=sqlite3.connect(self.path,timeout=.25)
        try:
            if write:
                db.execute('BEGIN IMMEDIATE')
                db.execute('CREATE TABLE IF NOT EXISTS plans(source TEXT PRIMARY KEY,payload TEXT NOT NULL)')
                db.execute('CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,slot TEXT UNIQUE NOT NULL,state TEXT NOT NULL,epoch INTEGER NOT NULL,payload TEXT NOT NULL)')
            yield db
            if write:db.commit()
        except BaseException:
            db.rollback();raise
        finally:db.close()

    def seed(self,db):
        from stockout_trigger import legacy_load
        for source,_ in SOURCES:
            if not db.execute('SELECT 1 FROM plans WHERE source=?',(source,)).fetchone():
                db.execute('INSERT INTO plans VALUES(?,?)',(source,encoded(legacy_load(self.folder,source))))

    def plans(self,db=None):
        if db is not None:return {source:json.loads(value) for source,value in db.execute('SELECT source,payload FROM plans')}
        if not self.path.is_file():
            from stockout_trigger import legacy_load
            return {source:legacy_load(self.folder,source) for source,_ in SOURCES}
        with self.connect() as opened:
            if not opened.execute("SELECT 1 FROM sqlite_master WHERE name='plans'").fetchone():
                from stockout_trigger import legacy_load
                return {source:legacy_load(self.folder,source) for source,_ in SOURCES}
            return self.plans(opened)

    def save(self,source,value,*,disable_legacy=False):
        with self.connect(True) as db:
            self.seed(db)
            db.execute('UPDATE plans SET payload=? WHERE source=?',(encoded(value),source))
            if disable_legacy:
                for old,_ in SOURCES[:2]:
                    previous=self.plans(db)[old]
                    db.execute('UPDATE plans SET payload=? WHERE source=?',(encoded({**previous,'enabled':False,'next_at':None}),old))

    def due(self,clock,prepare):
        from stockout_trigger import next_time
        clock=clock.astimezone(TZ)
        preliminary=self.plans()
        if not any(p['enabled'] and datetime.fromisoformat(p['next_at'])<=clock for p in preliminary.values()):return []
        groups={}
        for source,default_mode in SOURCES:
            value=preliminary[source]
            if not value['enabled']:continue
            when=datetime.fromisoformat(value['next_at']).astimezone(TZ)
            if when<=clock:groups.setdefault(when,[]).append((source,default_mode or value.get('mode'),value))
        prepared={when:prepare(when,parts) if when.date()==clock.date() else None for when,parts in groups.items()}
        with self.connect(True) as db:
            self.seed(db);current=self.plans(db)
            if current!=preliminary:return [] # Plan was edited while its inputs were read.
            out=[]
            for when,parts in sorted(groups.items()):
                slot=when.isoformat();rid=uuid.uuid4().hex;missed=prepared[when] is None
                row={'id':rid,'slot':slot,'day':when.date().isoformat(),'contributors':[x[0] for x in parts],
                     'request':prepared[when],'created':clock.isoformat()}
                state='MISSED' if missed else 'QUEUED'
                inserted=db.execute('INSERT OR IGNORE INTO runs VALUES(?,?,?,?,?)',(rid,slot,state,0,encoded(row))).rowcount
                if inserted:out.append({**row,'state':state,'epoch':0})
                for source,_,value in parts:
                    payload={**value,'next_at':next_time(value['at'],clock),'last_due_day':row['day']}
                    db.execute('UPDATE plans SET payload=? WHERE source=?',(encoded(payload),source))
            return out

    def entries(self,limit=50):
        if not self.path.is_file():return []
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='runs'").fetchone():return []
            return [{**json.loads(value),'state':state,'epoch':epoch} for value,state,epoch in db.execute(
                'SELECT payload,state,epoch FROM runs ORDER BY rowid DESC LIMIT ?',(limit,))]

    def pending(self):
        if not self.path.is_file():return False
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='runs'").fetchone():return False
            return bool(db.execute("SELECT 1 FROM runs WHERE state IN ('QUEUED','CLAIMED') LIMIT 1").fetchone())

    def claim(self):
        with self.connect(True) as db:
            row=db.execute("SELECT id,payload,epoch FROM runs WHERE state='QUEUED' ORDER BY slot LIMIT 1").fetchone()
            if not row:return None
            rid,payload,epoch=row
            db.execute("UPDATE runs SET state='CLAIMED',epoch=? WHERE id=?",(epoch+1,rid))
            return {**json.loads(payload),'epoch':epoch+1,'state':'CLAIMED'}

    def require_claim(self,rid,epoch):
        with self.connect() as db:
            if db.execute("SELECT 1 FROM runs WHERE id=? AND epoch=? AND state='CLAIMED'",(rid,epoch)).fetchone() is None:
                raise ValueError('缺货采集领取代次已失效')

    def recover(self):
        if not self.path.is_file():return
        with self.connect(True) as db:
            db.execute("UPDATE runs SET state='QUEUED',epoch=epoch+1 WHERE state='CLAIMED'")

    def finish(self,rid,epoch,state,message=''):
        with self.connect(True) as db:
            row=db.execute("SELECT payload FROM runs WHERE id=? AND epoch=? AND state='CLAIMED'",(rid,epoch)).fetchone()
            if row:
                value=json.loads(row[0]);value['message']=str(message)
                db.execute('UPDATE runs SET state=?,payload=? WHERE id=?',(state,encoded(value),rid))
