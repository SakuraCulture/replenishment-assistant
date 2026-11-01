"""Bundle committed AX code with hash-locked offline dependencies; no user state."""
import hashlib
import json
import re
import shutil
from pathlib import Path, PurePosixPath


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def relative(value):
    p = PurePosixPath(value)
    if not isinstance(value, str) or not value or p.is_absolute() or '\\' in value or ':' in value or any(x in ('', '.', '..') for x in value.split('/')):
        raise ValueError('Unsafe component member: '+str(value))
    return value


def inventory(root):
    root = Path(root)
    if root.is_symlink() or root.is_junction():
        raise ValueError('Component root cannot be a link')
    result = {}
    for p in sorted(root.rglob('*')):
        if p.is_symlink() or p.is_junction():
            raise ValueError('Component links are not packaged: '+str(p))
        if p.is_file():
            result[p.relative_to(root).as_posix()] = p
    return result


def verify(root):
    root = Path(root)
    manifest = json.loads((root/'integrity.manifest.json').read_text(encoding='utf-8'))
    if manifest.get('format') != 1 or not isinstance(manifest.get('files'), list):
        raise ValueError('Invalid component manifest')
    declared = {relative(f['relativePath']): f for f in manifest['files']}
    if len(declared) != len(manifest['files']):
        raise ValueError('Duplicate component manifest member')
    actual = inventory(root)
    if set(actual) != set(declared) | {'integrity.manifest.json'}:
        raise ValueError('Unexpected or missing component member')
    for name, f in declared.items():
        p = actual[name]
        if p.stat().st_size != f['bytes'] or sha(p) != f['sha256']:
            raise ValueError('Component checksum mismatch: '+name)
    return manifest


def stage(source, dependencies, destination, *, source_commit, app_version):
    """source is from git archive; dependencies may only supply locked runtime bytes."""
    source, dependencies, destination = map(Path, (source, dependencies, destination))
    lock = json.loads((source/'runtime-lock.json').read_text(encoding='utf-8'))
    if lock.get('format') != 1 or not re.fullmatch('[0-9a-f]{40}', source_commit):
        raise ValueError('Unrecognized runtime lock or source identity')
    expected = {relative(k): v for k, v in lock['files'].items()}
    if any(not k.startswith(('runtime/', 'node_modules/')) for k in expected):
        raise ValueError('Dependency lock cannot include source or user data')
    actual = {}
    for folder in ('runtime', 'node_modules'):
        actual.update({folder+'/'+k: v for k, v in inventory(dependencies/folder).items()})
    if set(actual) != set(expected):
        raise ValueError('Installed component runtime file set differs from lock')
    for name, p in actual.items():
        if sha(p) != expected[name]:
            raise ValueError('Installed component runtime changed: '+name)
    if sha(source/'package-lock.json') != lock['package_lock_sha256']:
        raise ValueError('Dependency declarations changed; regenerate verified runtime lock first')
    members = {}
    for folder in ('src', 'assets'):
        members.update({folder+'/'+k: v for k, v in inventory(source/folder).items()})
    for name in ('package.json', 'package-lock.json', 'NOTICE.txt', 'runtime-lock.json'):
        p = source/name
        if not p.is_file() or p.is_symlink():
            raise ValueError('Missing committed component file: '+name)
        members[name] = p
    members.update(actual)
    if any(k.startswith(('src/', 'assets/')) and re.search(r'(?:^|/)(?:\.env|Local State|Cookies|state\.sqlite|machine\.binding\.json)$', k) for k in members):
        raise ValueError('User state cannot enter the component bundle')
    destination.mkdir(parents=True, exist_ok=False)
    files = []
    for name, source_file in sorted(members.items()):
        p = destination/relative(name)
        p.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_file, p)
        digest = sha(p)
        if digest != (expected.get(name) or sha(source_file)):
            raise ValueError('Component changed during copy: '+name)
        if p.suffix.lower() in ('.mjs', '.js', '.json', '.md', '.txt', '.cmd', '.ps1', '.html', '.xml', '.yml', '.yaml'):
            body = p.read_text(encoding='utf-8', errors='strict')
            if re.search(r'(?:Signature|security-token|X-Amz-Signature)=[A-Za-z0-9%+/]{16,}|sk-proj-[A-Za-z0-9_-]{20,}|-----BEGIN (?:RSA )?PRIVATE KEY-----', body):
                raise ValueError('Potential credential in component text: '+name)
        files.append({'relativePath':name, 'bytes':p.stat().st_size, 'sha256':digest})
    manifest = {'format':1, 'upstream_version':lock['upstream_version'], 'application_version':app_version,
                'source_commit':source_commit, 'channel':'LOCAL_INTEGRATION_CANDIDATE',
                'runtime_lock_sha256':sha(source/'runtime-lock.json'), 'files':files}
    (destination/'integrity.manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    verify(destination)
    return {'source_commit':source_commit, 'files':len(files), 'manifest_sha256':sha(destination/'integrity.manifest.json'),
            'node_sha256':sha(destination/'runtime/node.exe'), 'runtime_lock_sha256':manifest['runtime_lock_sha256'],
            'contains_user_state':False, 'live_platform_tested':False}
