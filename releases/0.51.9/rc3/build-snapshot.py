"""User-authorized local EXE update without a Git commit.

Freeze git archive + individually hashed working files, then reuse the existing
builder's unchanged staging/PyInstaller steps. Snapshot IDs are explicitly not
Git commits. No real settings, ignored QA data or browser profiles are copied.
"""
import ast,hashlib,json,os,re,shutil,subprocess,sys,zipfile
from pathlib import Path
from datetime import datetime,timezone,timedelta
ROOT=Path.cwd().resolve()
QA=ROOT/'qa/unified_stockout_0519_20261112_01'
def sha(p):
    with p.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def write(p,v):p.write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n','utf-8',newline='\n')
def git(*a):return subprocess.check_output(['git',*a],cwd=ROOT)
assert (ROOT/'src/catalog_sync_ui.py').is_file()
assert not git('diff','--cached'),'Do not include or alter a preexisting index'
base=git('rev-parse','HEAD').decode().strip()
version=json.loads((ROOT/'src/version.json').read_text('utf-8'))['version'];build_id=sys.argv[1] if len(sys.argv)>1 else 'rc1'
assert re.fullmatch('[a-zA-Z0-9_-]+',build_id),'Invalid build ID'
build=ROOT/f'build_{version}_{build_id}';dist=ROOT/f'交付_{version}_{build_id}'
assert not build.exists() and not dist.exists(),'Existing deliveries/builds are never overwritten'
build.mkdir();source=build/'source';base_archive=build/'base-source.zip'
subprocess.run(['git','archive','--format=zip','-o',str(base_archive),base],cwd=ROOT,check=True)
with zipfile.ZipFile(base_archive) as z:z.extractall(source)
changed=sorted(set(git('ls-files','-m','-o','--exclude-standard','-z').decode('utf-8').split('\0'))-{''})
overlays={}
for name in changed:
    p=(ROOT/name).resolve();p.relative_to(ROOT)
    target=(source/name).resolve();target.relative_to(source)
    if p.is_file():
        data=p.read_bytes();target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data)
        overlays[name]=hashlib.sha256(data).hexdigest()
    elif target.is_file():target.unlink();overlays[name]=None
    else:raise ValueError('Unexpected working-tree member: '+name)
manifest={p.relative_to(source).as_posix():sha(p) for p in sorted(source.rglob('*')) if p.is_file()}
write(build/'source-manifest.json',manifest)
archive=build/'source.zip'
with zipfile.ZipFile(archive,'x',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
    for name in manifest:z.write(source/name,name)
snapshot=sha(archive);reference='snapshot-'+snapshot
for name,digest in overlays.items():
    assert (sha(ROOT/name) if digest else None)==digest,'Source changed during snapshot: '+name
info={'source_commit':reference,'source_commit_kind':'workspace_snapshot_not_git_commit','base_commit':base,
      'source_snapshot_sha256':snapshot,'source_archive_sha256':snapshot,'source_manifest_sha256':sha(build/'source-manifest.json'),
      'source_mode':'immutable uncommitted workspace snapshot: git base plus explicit hashed working files',
      'working_tree_files_sha256':overlays,'build_id':build_id,'version':version,
      'built_at':datetime.now(timezone(timedelta(hours=8))).isoformat(),'git_mutated':False}
# Reuse the exact current builder's component/dependency gates and PyInstaller
# options, but not its Git-commit input loader. Retain its original source too.
builder=source/'tools/build_from_commit.py';tree=ast.parse(builder.read_text('utf-8'))
body=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='main').body
index=lambda name:next(i for i,n in enumerate(body) if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id==name for t in n.targets))
start,index_env=index('component'),index('env')
ns=dict(globals(),commit=base,source=source,build=build,dist=dist,info=info,version=version)
exec(compile(ast.Module(body=body[start:index_env],type_ignores=[]),str(builder),'exec'),ns)
# Both component programs are unchanged; the artifact provenance manifest
# records this application snapshot rather than claiming a new Git commit.
jst=ns.get('jst_staged')
if jst:
    path=jst/'integrity.manifest.json';m=json.loads(path.read_text('utf-8'))
    m.update(source_commit=reference,source_commit_kind='workspace_snapshot_not_git_commit',base_commit=base,source_snapshot_sha256=snapshot)
    write(path,m);info['jst_catalog'].update(source_commit=reference,source_commit_kind='workspace_snapshot_not_git_commit',base_commit=base,
                                         source_snapshot_sha256=snapshot,manifest_sha256=sha(path))
    __import__('catalog_sync_bundle').verify_component(jst,ns['staged'])
info['build_procedure_sha256']=sha(builder)
write(build/'build_info.json',info);write(QA/'build-started.json',info)
exec(compile(ast.Module(body=body[index_env:-1],type_ignores=[]),str(builder),'exec'),ns)
assert (dist/'补货助手/补货助手.exe').is_file()
for name,digest in manifest.items():assert sha(source/name)==digest,'Frozen source changed during build: '+name
for name,digest in overlays.items():
    if name.startswith(('src/','components/','tools/','tests/')):assert (sha(ROOT/name) if digest else None)==digest,'Working runtime changed: '+name
assert git('rev-parse','HEAD').decode().strip()==base and not git('diff','--cached')
report={'passed':True,'delivery':str(dist),'build':str(build),'base_commit':base,'source_reference':reference,'source_snapshot_sha256':snapshot,
        'exe_sha256':sha(dist/'补货助手/补货助手.exe'),'working_tree_files':len(overlays),'snapshot_files':len(manifest),'git_mutated':False}
write(QA/'build-report.json',report);print(json.dumps(report,ensure_ascii=False))
