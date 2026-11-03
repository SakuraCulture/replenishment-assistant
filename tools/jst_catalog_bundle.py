"""Archive-only JST sources; use the already locked AX Node/dependencies, no user state."""
import json,shutil,sys
from pathlib import Path

def stage(source,ax,target,*,source_commit,app_version):
    source,ax,target=map(Path,(source,ax,target));sys.path.insert(0,str(source.parents[1]/'src'))
    from catalog_sync_bundle import digest,verify_component
    names=['package.json','upstream.json']+['src/'+p.name for p in sorted((source/'src').glob('*.mjs'))]
    target.mkdir(parents=True,exist_ok=False)
    for name in names:
        p=target/name;p.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source/name,p)
    shared=json.loads((ax/'integrity.manifest.json').read_text('utf-8'))
    # Hash runtime + libraries from the already verified shared component. No AX config changes.
    manifest={'schema':1,'source_commit':source_commit,'app_version':app_version,'files':{n:digest(target/n) for n in names},'shared':{f['relativePath']:f['sha256'] for f in shared['files']}}
    (target/'integrity.manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),'utf-8');verify_component(target,ax)
    return {'manifest_sha256':digest(target/'integrity.manifest.json'),'files':len(names),'source_commit':source_commit,'contains_user_state':False}
