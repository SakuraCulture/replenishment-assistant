"""Verified database backup. Source is read-only; publication never overwrites."""
import sqlite3,os,uuid
from pathlib import Path
from contextlib import closing
import storage_chunks as chunks
from maintenance_storage import Editor
from runtime_support import checkpoint
from export_progress import RowProgress

def backup(source,destination,progress=lambda _:None,cancel=None):
    source=Path(source).resolve();destination=Path(destination).resolve()
    if source==destination or destination.exists():raise ValueError('请使用不存在的新文件名，不能覆盖原库或旧副本')
    if not source.is_file():raise ValueError('源数据库不存在')
    temporary=destination.with_name('.'+destination.name+'.'+uuid.uuid4().hex+'.tmp')
    def report(value):checkpoint(cancel);progress(value)
    counts={}
    try:
        with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)) as src,closing(sqlite3.connect(temporary)) as target,closing(sqlite3.connect(':memory:')) as scratch:
            src.backup(target,pages=1024,progress=lambda status,left,total:report(RowProgress('复制数据库至独立临时文件',total-left,total)))
            if target.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('数据库副本完整性检查失败')
            chunks.require_current(target,cancel);editor=Editor(target,scratch,report,cancel)
            tables={r[0] for r in target.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in ('jobs','normal_revisions','normal_work_revisions','scheduled_values'):
                if table not in tables:continue
                total=target.execute('SELECT COUNT(*) FROM '+table).fetchone()[0];counts[table]=0
                fields='payload'+(',digest' if table!='normal_work_revisions' else '')
                for record in target.execute('SELECT '+fields+' FROM '+table+' ORDER BY rowid'):
                    report(RowProgress('校验备份 '+table,counts[table],total))
                    manifest=chunks.require_manifest(record[0])
                    if editor.checksum(manifest['tree'])!=(manifest['digest'],manifest['size']):raise ValueError('备份快照校验失败')
                    if table in ('jobs','scheduled_values') and manifest['digest']!=record[1]:raise ValueError('作业快照与索引不一致')
                    if table=='normal_revisions':
                        from normal_orders import fingerprint_owned
                        actual=fingerprint_owned(editor.get(manifest['tree'],('result',)),editor.get(manifest['tree'],('new_focus',)))
                        if manifest.get('binding')!=record[1] or actual!=record[1]:raise ValueError('正常单业务指纹不一致')
                    counts[table]+=1
                report(RowProgress('校验备份 '+table,total,total))
        checkpoint(cancel);os.link(temporary,destination)
        return {'path':str(destination),'mode':'backup','records':counts,'source':str(source),'source_modified':False}
    finally:temporary.unlink(missing_ok=True)

def cli(arguments):
    import argparse,json
    parser=argparse.ArgumentParser();parser.add_argument('mode',choices=['backup']);parser.add_argument('source',type=Path);parser.add_argument('destination',type=Path);parser.add_argument('--report',type=Path,required=True)
    args=parser.parse_args(arguments)
    if args.report.resolve() in (args.source.resolve(),args.destination.resolve()):raise ValueError('报告路径不能与数据库相同')
    with args.report.open('x',encoding='utf-8') as report:
        try:result={'passed':True,**backup(args.source,args.destination)}
        except Exception as exc:result={'passed':False,'error':str(exc),'source':str(args.source),'destination':str(args.destination)}
        report.write(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if result['passed'] else 1
