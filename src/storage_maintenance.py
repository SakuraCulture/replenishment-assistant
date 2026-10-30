"""Copy-only storage conversion. Source is always read-only; never auto-restore."""
import sqlite3,os,uuid,zlib,hashlib
from pathlib import Path
from contextlib import closing
import storage_chunks as chunks
import work_repository as wr
from runtime_support import checkpoint
from export_progress import RowProgress

def convert(source,destination,mode,progress=lambda _:None,cancel=None):
    if mode not in ('upgrade','legacy'):raise ValueError('未知存储副本类型')
    source=Path(source).resolve();destination=Path(destination).resolve()
    if source==destination or destination.exists():raise ValueError('请使用不存在的新文件名，不能覆盖原库或旧副本')
    if not source.is_file():raise ValueError('源数据库不存在')
    temporary=destination.with_name('.'+destination.name+'.'+uuid.uuid4().hex+'.tmp')
    def report(value):checkpoint(cancel);progress(value)
    counts={'jobs':0,'normal_revisions':0,'normal_work_revisions':0};checks=[]
    try:
        with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)) as src,closing(sqlite3.connect(temporary)) as target:
            src.backup(target,pages=1024,progress=lambda status,left,total:report(RowProgress('复制数据库至独立临时文件',total-left,total)))
            if target.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('数据库副本完整性检查失败')
            tables={r[0] for r in target.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            existing=chunks.known(target)
            with target:
                if mode=='upgrade':chunks.schema(target)
                for table in counts:
                    if table not in tables:continue
                    ids=[r[0] for r in target.execute('SELECT rowid FROM '+table+' ORDER BY rowid')]
                    for i,rowid in enumerate(ids):
                        report(RowProgress('转换 '+table,i,len(ids)))
                        fields='payload'+(',digest' if table!='normal_work_revisions' else '')
                        record=target.execute('SELECT '+fields+' FROM '+table+' WHERE rowid=?',(rowid,)).fetchone()
                        expected=record[1] if table=='jobs' else None
                        binding=record[1] if table=='normal_revisions' else None
                        value=chunks.load(record[0],target,expected=expected,binding=binding,progress=report)
                        if binding:
                            from normal_orders import fingerprint_owned
                            if fingerprint_owned(value['result'],value.get('new_focus'))!=binding:raise ValueError('正常单业务指纹不一致，未转换')
                        if mode=='upgrade':
                            pack=chunks.prepare(value,existing,report,binding=binding);converted=chunks.put(target,pack);existing.update(pack.chunks)
                            digest=pack.digest
                        else:
                            report('编码旧格式完整快照…');raw=wr.dumps(value).encode();digest=hashlib.sha256(raw).hexdigest();converted=zlib.compress(raw);del raw
                        # Exact canonical digest covers every retained value/type/order.
                        restored=chunks.load(converted,target,expected=digest,progress=report)
                        if hashlib.sha256(wr.dumps(restored).encode()).hexdigest()!=digest:raise ValueError('转换回读不一致')
                        del restored,value
                        target.execute('UPDATE '+table+' SET payload=? WHERE rowid=?',(converted,rowid));counts[table]+=1;checks.append({'table':table,'rowid':rowid,'sha256':digest})
                        report(RowProgress('转换 '+table,i+1,len(ids)))
                if mode=='legacy':
                    target.execute('DROP TABLE IF EXISTS snapshot_chunks');target.execute('DROP TABLE IF EXISTS snapshot_storage_meta')
                else:
                    target.execute("INSERT OR REPLACE INTO snapshot_storage_meta VALUES('copy_source',?)",(str(source),))
            if target.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('转换后数据库完整性检查失败')
            if mode=='legacy':target.execute('VACUUM')
        checkpoint(cancel)
        os.link(temporary,destination)  # exclusive, atomic publication on same volume
        return {'path':str(destination),'mode':mode,'records':counts,'checks':checks,'source':str(source),'source_modified':False}
    finally:
        temporary.unlink(missing_ok=True)

def cli(arguments):
    import argparse,json
    parser=argparse.ArgumentParser();parser.add_argument('mode',choices=['upgrade','legacy']);parser.add_argument('source',type=Path);parser.add_argument('destination',type=Path);parser.add_argument('--report',type=Path,required=True)
    args=parser.parse_args(arguments)
    if args.report.resolve() in (args.source.resolve(),args.destination.resolve()):raise ValueError('报告路径不能与数据库相同')
    with args.report.open('x',encoding='utf-8') as report:
        try:result={'passed':True,**convert(args.source,args.destination,args.mode)}
        except Exception as exc:result={'passed':False,'error':str(exc),'source':str(args.source),'destination':str(args.destination)}
        report.write(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if result['passed'] else 1
