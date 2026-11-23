"""Verify isolated component bytes and the exact shared read-only AX libraries."""
import hashlib
import json
import re
from pathlib import Path


MANIFEST = 'integrity.manifest.json'


class ComponentIntegrityError(ValueError):
    def __init__(self, jst, ax, manifest, problems):
        super().__init__('聚水潭组件文件校验未通过。请查看详细信息，或从「显示与帮助」核对原程序目录。')
        self.details = '\n'.join([describe_component(jst, ax, manifest), '', *problems])


def digest(path):
    with Path(path).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()


def describe_component(jst, ax, manifest=None):
    manifest = manifest if isinstance(manifest, dict) else {}
    internal = jst.parent.parent
    try:
        build = json.loads((internal/'build_info.json').read_text('utf-8-sig'))
    except (OSError, ValueError):
        build = {}
    if not isinstance(build, dict):
        build = {}
    return '\n'.join([
        f'程序目录：{internal.parent}',
        f'构建标识：{build.get("build_id") or "未留存"}',
        f'程序源码：{build.get("source_commit") or "未留存"}',
        f'组件源码：{manifest.get("source_commit") or "未留存"}',
        f'聚水潭组件：{jst}',
        f'共用组件：{ax}',
    ])


def verify_component(jst, ax):
    jst, ax = Path(jst).absolute(), Path(ax).absolute()
    try:
        manifest = json.loads((jst/MANIFEST).read_text('utf-8'))
    except FileNotFoundError as exc:
        raise ComponentIntegrityError(jst, ax, None, [f'缺失：聚水潭/{MANIFEST}']) from exc
    except (OSError, ValueError) as exc:
        raise ComponentIntegrityError(jst, ax, None, [f'清单无法读取或解析：聚水潭/{MANIFEST}']) from exc
    if not isinstance(manifest, dict) or manifest.get('schema') != 1 or any(
        not isinstance(manifest.get(key), dict) or any(
            not name or not isinstance(sha, str) or not re.fullmatch('[0-9a-f]{64}', sha)
            for name, sha in manifest[key].items()
        ) for key in ('files', 'shared')
    ):
        raise ComponentIntegrityError(jst, ax, manifest, [f'清单格式异常：聚水潭/{MANIFEST}'])

    listed = set(manifest['files']) | {MANIFEST}
    actual = {p.relative_to(jst).as_posix() for p in jst.rglob('*') if p.is_file()}
    problems = [f'多余：聚水潭/{name}' for name in sorted(actual - listed)]
    for root, key, label in ((jst, 'files', '聚水潭'), (ax, 'shared', '共用组件')):
        base = root.resolve()
        for name, sha in sorted(manifest[key].items()):
            file_label = f'{label}/{name}'
            try:
                file = (root/name).resolve()
                if not file.is_relative_to(base):
                    problems.append(f'路径超出组件目录：{file_label}')
                    continue
                if key == 'files' and name not in actual:
                    problems.append(f'缺失：{file_label}')
                    continue
                if digest(file) != sha:
                    problems.append(f'内容不符：{file_label}')
            except FileNotFoundError:
                problems.append(f'缺失：{file_label}')
            except (OSError, RuntimeError):
                problems.append(f'无法读取：{file_label}')
    if problems:
        raise ComponentIntegrityError(jst, ax, manifest, problems)
    return manifest
