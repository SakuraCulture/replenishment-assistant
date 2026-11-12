"""Complete local order area, verified portable archive and shutdown-only restore.

No platform calls. Browser identities/configuration, cookies and analysis modules
are not exported. Existing target data is retained as a complete sibling backup.
"""
import hashlib,json,os,re,shutil,sqlite3,tempfile,uuid,zipfile,zlib
from contextlib import ExitStack,closing
from pathlib import Path,PurePosixPath
from stockout_common import encoded,file_hash,atomic_json,now,resource_check
from runtime_support import checkpoint
from application_reset import validate_root

FORMAT='replenishment-orders/1'
ROOT_FILES={'workbench.sqlite3','workbench.json','must_sell.json','store_budgets.json','new_product_focus.json','store_supply.json'}
DERIVED={'archive_index.sqlite3','lifecycle_index.sqlite3'}
DBS={'workbench.sqlite3','aoxiang/workflows.sqlite3','aoxiang/jobs.sqlite3','aoxiang/component-state/state.sqlite'}
STATE_FILES={'state.sqlite','state.guard.json'}
STATE_DIRS={'command-files','downloads','assets','reports','purchase-files','purchase-creates','purchase-receipts','exports','evidence','receipts'}
JST_FILES={'jobs.json','store.guard.json'}
SUFFIXES={'.xlsx','.csv','.xls','.pdf'}

def owned(name):
    p=PurePosixPath(name)
    if name!=p.as_posix() or p.is_absolute() or any(x in ('..','.','') for x in p.parts) or '\\' in name or ':' in name:return False
    parts=p.parts
    if name in ROOT_FILES or name in DBS or name=='aoxiang/jobs.sqlite3.guard':return True
    if parts[0]=='warehouse_catalogs':return p.suffix=='.json'
    if parts[0]=='migration-assets':return len(parts)==3 and bool(re.fullmatch(r'[a-f0-9]{64}',parts[1])) and p.suffix.lower() in SUFFIXES
    if len(parts)>=3 and parts[:2]==('aoxiang','component-state'):
        excluded={'config','recovery','temporary','logs','browser','profiles','profile','user-data','Cache','Code Cache','node_modules'}
        return (len(parts)==3 and parts[2] in STATE_FILES) or (len(parts)>3 and not set(parts[2:-1])&excluded and p.suffix.lower() in SUFFIXES|{'.json','.html','.png','.txt'})
    if len(parts)>=2 and parts[0] in ('jst_catalog','jst_catalog_sync'):return (len(parts)==2 and parts[1] in JST_FILES) or (parts[1]=='downloads' and p.suffix.lower() in SUFFIXES|{'.json'})
    return False

def tree(root):
    root=Path(root).resolve();result=[]
    for folder,dirs,files in os.walk(root,followlinks=False):
        parent=Path(folder);parts=parent.relative_to(root).parts
        def include(name):
            if not parts:return name in {'warehouse_catalogs','aoxiang','jst_catalog','jst_catalog_sync','migration-assets'}
            if parts==('aoxiang',):return name=='component-state'
            if parts in (('jst_catalog',),('jst_catalog_sync',)):return name=='downloads'
            return name not in {'config','recovery','temporary','logs','browser','profiles','profile','user-data','Cache','Code Cache','node_modules'}
        dirs[:]=[n for n in dirs if include(n) and not (parent/n).is_symlink() and not (parent/n).is_junction()]
        for name in files:
            path=parent/name
            if path.is_symlink():continue
            if owned(path.relative_to(root).as_posix()):result.append(path)
    return sorted(result)

def db_copy(source,target,*,main=False,cancel=None,progress=lambda _:None):
    target.parent.mkdir(parents=True,exist_ok=True)
    if main:
        from storage_maintenance import backup
        value=backup(source,target,progress,cancel);verify_originals(target,cancel);return value
    with closing(sqlite3.connect(Path(source).resolve().as_uri()+'?mode=ro',uri=True)) as src,closing(sqlite3.connect(target)) as dst:
        src.backup(dst,pages=1024,progress=lambda *_:checkpoint(cancel))
        if dst.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('数据库完整性校验失败：'+Path(source).name)
    return {}

def verify_originals(path,cancel=None):
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)) as db:
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ('source_files','purchase_files'):
            if table not in tables:continue
            for rowid,sha in db.execute('SELECT rowid,sha FROM '+table):
                h=hashlib.sha256()
                with db.blobopen(table,'content',rowid,readonly=True) as blob:
                    while data:=blob.read(512*1024):checkpoint(cancel);h.update(data)
                if h.hexdigest()!=sha:raise ValueError('库内原件摘要校验失败：'+table+' / '+str(sha))

def info(path):
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)) as db:
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")};counts={}
        for table in ('normal_orders','normal_revisions','normal_work_revisions','jobs','lifecycle_routes','lifecycle_versions','purchase_documents','purchase_versions','jst_generations','jst_baselines','source_files','stores','presets','deleted_order_tokens'):
            if table in tables:counts[table]=db.execute('SELECT count(*) FROM '+table).fetchone()[0]
    return counts

def referenced_files(root,dbpath,cancel):
    """Inspect bounded content blocks; originals inside source_files stay in DB."""
    result=set()
    def scan(value):
        if isinstance(value,dict):
            for v in value.values():scan(v)
        elif isinstance(value,list):
            for v in value:scan(v)
        elif isinstance(value,str) and len(value)<32768 and Path(value).is_absolute() and Path(value).suffix.lower() in SUFFIXES:result.add(value)
    for path in tree(root):
        checkpoint(cancel)
        if path.suffix=='.json' and path.stat().st_size<32*1024**2:
            scan(json.loads(path.read_text('utf-8-sig')))
    with closing(sqlite3.connect(Path(dbpath).resolve().as_uri()+'?mode=ro',uri=True)) as db:
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'snapshot_chunks' in tables:
            import work_repository as wr
            for raw, in db.execute('SELECT data FROM snapshot_chunks'):
                checkpoint(cancel);scan(wr.loads(zlib.decompress(raw)))
        for table in ('purchase_versions','lifecycle_versions'):
            if table in tables:
                for raw, in db.execute('SELECT payload FROM '+table):
                    checkpoint(cancel)
                    if isinstance(raw,str):scan(json.loads(raw))
    for name in DBS-{'workbench.sqlite3'}:
        path=Path(root)/name
        if not path.is_file():continue
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db:
            for table, in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
                quoted='"'+table.replace('"','""')+'"'
                cols={r[1] for r in db.execute('PRAGMA table_info('+quoted+')')}&{'payload','command','context','record'}
                for col in cols:
                    for raw, in db.execute('SELECT "'+col+'" FROM '+quoted):
                        checkpoint(cancel)
                        if isinstance(raw,str):scan(json.loads(raw))
    return result

def export(root,destination,progress=lambda _:None,cancel=None,*,catalog_state=None):
    from version_info import VERSION,MODEL_VERSION
    root=validate_root(root);target=Path(destination).resolve()
    if target.exists() or target.suffix.lower()!='.zip':raise ValueError('请选择尚不存在的.zip迁移包')
    if target.is_relative_to(root):raise ValueError('迁移包请保存到应用资料目录之外')
    sources=tree(root)
    if root/'workbench.sqlite3' not in sources:raise ValueError('没有完整订单库，不能生成空迁移包')
    target.parent.mkdir(parents=True,exist_ok=True);resource_check(target.parent,sum(p.stat().st_size for p in sources)*2)
    temp=target.with_name('.migration-'+uuid.uuid4().hex+'.zip')
    try:
        with tempfile.TemporaryDirectory(prefix='orders-',dir=target.parent) as working,ExitStack() as stack:
            work=Path(working);entries=[];external={};missing=[]
            from catalog_migration import freeze
            catalogs={name:freeze(root,stack,catalog_state if name=='jst_catalog_sync' else None,folder_name=name)
                      for name in ('jst_catalog_sync','jst_catalog')}
            # Freeze writers in every included SQLite file before the first copy.
            # Independent read-only connections can then use SQLite backup in
            # rollback-journal or WAL mode without copying a live WAL file.
            for path in sources:
                if path.relative_to(root).as_posix() in DBS:
                    db=stack.enter_context(closing(sqlite3.connect(path.as_uri()+'?mode=rw',uri=True,timeout=1,isolation_level=None)));db.execute('BEGIN IMMEDIATE')
            for path in sources:
                checkpoint(cancel);name=path.relative_to(root).as_posix();dest=work/name;dest.parent.mkdir(parents=True,exist_ok=True);progress('留存：'+name)
                catalog=catalogs.get(path.relative_to(root).parts[0])
                if catalog is not None and path.name=='jobs.json' and len(path.relative_to(root).parts)==2:atomic_json(dest,catalog)
                elif catalog is not None and path.name=='store.guard.json' and len(path.relative_to(root).parts)==2:atomic_json(dest,{'schemaVersion':1,'storeId':catalog['storeId']})
                elif name in DBS:db_copy(path,dest,main=name=='workbench.sqlite3',cancel=cancel,progress=progress)
                else:
                    before=file_hash(path);shutil.copyfile(path,dest)
                    if file_hash(dest)!=before or file_hash(path)!=before:raise ValueError('文件在导出期间变化，请安全停止原任务后重试：'+name)
                entries.append({'path':name,'size':dest.stat().st_size,'sha256':file_hash(dest)});external[str(path)]=name
            prior_map=root/'迁入原文件映射.json'
            if prior_map.is_file():
                previous=json.loads(prior_map.read_text('utf-8'));included={e['path'] for e in entries}
                for old,relative in previous.get('paths',{}).items():
                    if relative in included and owned(relative):external[old]=relative
            for value in sorted(referenced_files(work,work/'workbench.sqlite3',cancel)):
                if value in external:continue
                path=Path(value)
                if path.is_relative_to(root) and path in sources:continue
                if not path.is_file() or path.is_symlink():missing.append(value);continue
                sha=file_hash(path);name='migration-assets/'+sha+'/'+path.name;dest=work/name;dest.parent.mkdir(parents=True,exist_ok=True)
                if not dest.exists():
                    shutil.copyfile(path,dest)
                    if file_hash(dest)!=sha or file_hash(path)!=sha:raise ValueError('外部原文件在导出期间变化，未生成迁移包')
                    entries.append({'path':name,'size':dest.stat().st_size,'sha256':sha})
                external[value]=name
            manifest={'format':FORMAT,'created':now(),'app_version':VERSION,'model_version':MODEL_VERSION,'storage':2,
                'scope':'全部本地订单、全部修订、关联单据、原件、库存分配、平台关联与防重事实、草稿、参数及清单；不按当前页筛选',
                'counts':info(work/'workbench.sqlite3'),'entries':entries,'external_map':external,'missing_external':missing,
                'excluded':'浏览器登录、机器连接配置、凭据、AI密钥、独立缺货率分析、缓存及旧备份'}
            with zipfile.ZipFile(temp,'x',compression=zipfile.ZIP_DEFLATED,compresslevel=3,allowZip64=True) as book:
                book.writestr('manifest.json',encoded(manifest))
                for entry in entries:checkpoint(cancel);book.write(work/entry['path'],entry['path'])
            checkpoint(cancel)
            from export_files import publish_new
            publish_new(temp,target)
        return {'path':str(target),'sha256':file_hash(target),'manifest':manifest}
    finally:temp.unlink(missing_ok=True)

def manifest(path):
    with zipfile.ZipFile(path) as book:
        names=book.namelist()
        if len(names)!=len(set(names)) or len(names)>50001:raise ValueError('迁移包条目重复或数量异常')
        if 'manifest.json' not in names or book.getinfo('manifest.json').file_size>16*1024**2:raise ValueError('迁移包清单缺失或过大')
        value=json.loads(book.read('manifest.json'))
        if value.get('format')!=FORMAT or value.get('storage')!=2:raise ValueError('迁移包格式或存储版本不兼容')
        from version_info import VERSION
        if tuple(map(int,value['app_version'].split('.')))>tuple(map(int,VERSION.split('.'))):raise ValueError('迁移包来自更高版本，请先升级补货助手')
        entries=value['entries'];paths=[e['path'] for e in entries]
        if len(paths)!=len({p.casefold() for p in paths}) or set(names)!=set(paths)|{'manifest.json'} or 'workbench.sqlite3' not in paths:raise ValueError('迁移包文件集合不完整或有重复条目')
        for entry in entries:
            name=entry['path'];meta=book.getinfo(name)
            if not owned(name) or meta.file_size!=entry['size'] or not re.fullmatch('[0-9a-f]{64}',entry['sha256']):raise ValueError('迁移包路径、大小或摘要无效：'+name)
            if meta.external_attr>>16 & 0o170000==0o120000:raise ValueError('迁移包不允许链接文件')
        for old,new in value.get('external_map',{}).items():
            if not Path(old).is_absolute() or new not in paths or not owned(new):raise ValueError('原文件迁移映射无效')
    return value

def prepare(root,package,progress=lambda _:None,cancel=None):
    root=validate_root(root);package=Path(package).resolve();before=file_hash(package);value=manifest(package)
    resource_check(root.parent,sum(e['size'] for e in value['entries'])*2)
    nonce=uuid.uuid4().hex;stage=root.with_name('.'+root.name+'.order-import-'+nonce);stage.mkdir()
    try:
        with zipfile.ZipFile(package) as book:
            for entry in value['entries']:
                checkpoint(cancel);target=stage/entry['path'];target.parent.mkdir(parents=True,exist_ok=True);h=hashlib.sha256();progress('校验并准备：'+entry['path'])
                with book.open(entry['path']) as source,target.open('xb') as dst:
                    while block:=source.read(512*1024):checkpoint(cancel);h.update(block);dst.write(block)
                    dst.flush();os.fsync(dst.fileno())
                if h.hexdigest()!=entry['sha256']:raise ValueError('迁移包内容校验失败：'+entry['path'])
        for entry in value['entries']:
            if entry['path'] in DBS:
                db=stage/entry['path'];verified=db.with_name(db.name+'.verified')
                db_copy(db,verified,main=entry['path']=='workbench.sqlite3',cancel=cancel,progress=progress);verified.unlink()
        if file_hash(package)!=before:raise ValueError('迁移包在读取期间变化，未准备导入')
        contents={}
        for entry in value['entries']:contents.setdefault(entry['sha256'],[]).append(entry['path'])
        atomic_json(stage/'迁入原文件映射.json',{'version':1,'paths':value.get('external_map',{}),'contents':contents})
        plan={'root':str(root),'stage':str(stage),'backup':str(root.with_name(root.name+'.订单导入备份-'+nonce)),
                'new':str(root.with_name('.'+root.name+'.order-new-'+nonce)),'nonce':nonce,'package':str(package),'package_sha256':before,'manifest':value}
        from migration_status import record
        record(plan,'PREPARED');return plan
    except BaseException:shutil.rmtree(stage);raise

def checked_plan(plan):
    root=validate_root(plan['root']);nonce=plan['nonce']
    if not re.fullmatch('[a-f0-9]{32}',nonce):raise ValueError('导入操作身份无效')
    expected={'stage':'.'+root.name+'.order-import-'+nonce,'new':'.'+root.name+'.order-new-'+nonce,'backup':root.name+'.订单导入备份-'+nonce}
    for key,name in expected.items():
        path=Path(plan[key]).absolute()
        if path.parent.resolve()!=root.parent or path.name!=name or path.is_symlink() or path.exists() and path.is_junction():raise ValueError('导入工作目录已变化，未操作')
    return root

def discard(plan):
    checked_plan(plan);stage=Path(plan['stage'])
    from migration_status import latest,record
    value=latest(plan['root'])
    if not value or value['nonce']!=plan['nonce'] or value['state']!='COMPLETED':record(plan,'CANCELLED')
    if stage.exists():shutil.rmtree(stage)

def pause_imported(root):
    """Pause dispatch, preserve original states/receipts in a migration audit."""
    audit=[]
    def pause(db,table,key,payload_key='payload',state_key='state',active=()):
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name=? AND type='table'",(table,)).fetchone():return
        for ident,old,raw in db.execute(f'SELECT {key},{state_key},{payload_key} FROM {table}').fetchall():
            if old not in active:continue
            value=json.loads(raw);audit.append({'table':table,'id':ident,'state':old});value['state']='PAUSED';value['migration_paused']=True
            db.execute(f'UPDATE {table} SET {state_key}=?,{payload_key}=? WHERE {key}=?',('PAUSED',encoded(value),ident))
    main=Path(root)/'workbench.sqlite3'
    with closing(sqlite3.connect(main)) as db,db:
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'jst_tasks' in tables:
            audit.extend({'table':'jst_tasks','id':r[0],'state':r[1]} for r in db.execute("SELECT task,status FROM jst_tasks WHERE status IN ('pending','prepared')"))
            db.execute("UPDATE jst_tasks SET status='paused',message='迁入后已暂停；请在原分配批次核对并继续' WHERE status IN ('pending','prepared')")
        if 'jst_batches' in tables:
            audit.extend({'table':'jst_batches','id':r[0],'state':r[1]} for r in db.execute("SELECT batch_id,status FROM jst_batches WHERE status='active'"))
            db.execute("UPDATE jst_batches SET status='paused',epoch=epoch+1 WHERE status='active'")
        pause(db,'scheduled_runs','id',active=('QUEUED','RUNNING','PAUSING'))
        if 'scheduled_arrangements' in tables:
            for ident,raw in db.execute('SELECT id,payload FROM scheduled_arrangements').fetchall():
                value=json.loads(raw)
                if value.get('enabled'):audit.append({'table':'scheduled_arrangements','id':ident,'enabled':True});value.update(enabled=False,next_at=None);db.execute('UPDATE scheduled_arrangements SET payload=? WHERE id=?',(encoded(value),ident))
    workflows=Path(root)/'aoxiang'/'workflows.sqlite3'
    if workflows.exists():
        with closing(sqlite3.connect(workflows)) as db,db:
            pause(db,'ax_workflows','id',active=('QUEUED','RUNNING','YIELDED'))
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='ax_schedules'").fetchone():
                for ident,raw in db.execute('SELECT id,payload FROM ax_schedules').fetchall():
                    value=json.loads(raw)
                    if value.get('enabled'):audit.append({'table':'ax_schedules','id':ident,'enabled':True});value['enabled']=False;db.execute('UPDATE ax_schedules SET payload=? WHERE id=?',(encoded(value),ident))
    atomic_json(Path(root)/'订单迁入暂停记录.json',{'at':now(),'paused':audit,'notice':'平台请求、回执、防重事实保持；迁入后不自动继续，先核验连接与原平台结果。'})

def journal(root):return root.parent/('.'+root.name+'.order-import.json')

def _recover(root):
    root=validate_root(root);path=journal(root)
    if not path.exists():return None
    plan=json.loads(path.read_text('utf-8'));checked_plan(plan)
    if Path(plan['root']).resolve()!=root:raise ValueError('订单导入恢复目录不一致')
    backup=Path(plan['backup']);receipt=root/'订单迁入完成.json'
    if receipt.is_file() and json.loads(receipt.read_text('utf-8')).get('nonce')==plan['nonce']:
        result={'state':'COMPLETED','backup':str(backup)}
        from migration_status import record
        record(plan,'COMPLETED',result=result);path.unlink();return result
    if backup.exists():
        if root.exists():
            failed=root.with_name('.'+root.name+'.order-failed-'+plan['nonce'])
            if failed.exists():raise ValueError('导入恢复证据目录已存在，请保留现场')
            root.rename(failed)
        backup.rename(root)
    result={'state':'ROLLED_BACK','root':str(root)}
    from migration_status import record
    record(plan,'FAILED',phase='REPLACING',error=RuntimeError('目录替换未完成，已恢复原库；迁入材料和替换工作目录保留'),result=result)
    path.unlink();return result


def recover(root):
    root=validate_root(root)
    if not journal(root).exists():return None
    from application_reset import _exclusive
    with _exclusive(root):return _recover(root)

def _apply(plan):
    """Only after accepted GUI close; never deletes the target backup."""
    root=checked_plan(plan);stage=Path(plan['stage']);backup=Path(plan['backup']);new=Path(plan['new'])
    from application_reset import _exclusive,_check_other_processes
    if not stage.is_dir() or backup.exists() or new.exists():raise ValueError('导入准备或目标目录已变化，原资料未替换')
    for entry in plan['manifest']['entries']:
        if not owned(entry['path']):raise ValueError('准备的迁入路径无效')
        if not (stage/entry['path']).resolve().is_relative_to(stage.resolve()):raise ValueError('准备的迁入文件越出临时目录')
        if file_hash(stage/entry['path'])!=entry['sha256']:raise ValueError('准备的迁入文件已变化，未替换资料')
    with _exclusive(root):
        from migration_status import record
        record(plan,'APPLYING',phase='WAITING_LOCKS')
        _check_other_processes();resource_check(root.parent,sum(p.stat().st_size for p in root.rglob('*') if p.is_file()))
        from catalog_migration import local_connections,restore as restore_catalog
        catalog_local=local_connections(root)
        connections=[]
        try:
            for name in sorted(DBS):
                file=root/name
                if not file.exists():continue
                db=sqlite3.connect(file.resolve().as_uri()+'?mode=rw',uri=True,timeout=.3,isolation_level=None);connections.append(db);db.execute('BEGIN EXCLUSIVE')
        except sqlite3.DatabaseError as exc:
            raise ValueError('目标订单或关联作业库仍被占用或未能安全锁定；请等原任务安全结束，资料未替换') from exc
        finally:
            for db in connections:db.close()
        # Keep all target-local connection/analysis/preferences; replace only
        # the explicitly owned order area. The old whole directory is backed up.
        def ignore(folder,names):
            ignored=[]
            for name in names:
                p=Path(folder)/name;relative=p.relative_to(root).as_posix()
                if p.is_symlink() or p.is_junction():raise ValueError('目标资料含链接，未迁入')
                if p.is_file() and (owned(relative) or relative in DERIVED or relative in {db+suffix for db in DBS|DERIVED for suffix in ('-wal','-shm','-journal')}):ignored.append(name)
            return ignored
        record(plan,'APPLYING',phase='COPYING')
        shutil.copytree(root,new,ignore=ignore)
        try:
            for entry in plan['manifest']['entries']:
                target=new/entry['path'];target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(stage/entry['path'],target)
            shutil.copyfile(stage/'迁入原文件映射.json',new/'迁入原文件映射.json');pause_imported(new)
            restore_catalog(new,root,plan['manifest'],catalog_local)
            atomic_json(new/'订单迁入完成.json',{'nonce':plan['nonce'],'at':now(),'package_sha256':plan['package_sha256'],'backup':str(backup),'scope':plan['manifest']['scope']})
            record(plan,'APPLYING',phase='REPLACING');atomic_json(journal(root),plan)
            root.rename(backup);new.rename(root)
        except BaseException:
            if journal(root).exists():_recover(root)
            raise
        result=_recover(root);discard(plan);return result


def apply(plan):
    from migration_status import record,latest
    checked_plan(plan);record(plan,'APPLYING',phase='VALIDATING')
    try:return _apply(plan)
    except BaseException as exc:
        value=latest(plan['root'])
        if value and value['nonce']==plan['nonce'] and value['state']=='COMPLETED':
            record(plan,'COMPLETED',phase='CLEANUP_PENDING',error=exc,result=value['result']);return value['result']
        record(plan,'FAILED',phase=value.get('phase','APPLYING') if value else 'APPLYING',error=exc)
        raise
