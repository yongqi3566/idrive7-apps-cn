#!/usr/bin/env python3
"""Build SDK-free, fixed-language packages and a catalog for the MGU Store.

    python3 kit/tools/publish_store.py --version 1.0.1 --repository owner/repo

The catalog release points at packages in a separate release. Updating its
catalog.json is the last publishing step, after all packages are available.
"""
import argparse
import copy
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import tarfile
from urllib.parse import quote

import build
from localize import languages, localize_spec
from make_kit import prebuilt_hash, source_hash

ROOT = Path(build.ROOT)
SEMVER = re.compile(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)\Z')
ID = re.compile(r'[a-z0-9][a-z0-9_-]{0,63}\Z')
META = ('title', 'description', 'author', 'texts', 'requires', 'mguHmi',
        'optional', 'uninstallable', 'mguProvides', 'mguNeeds')


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def safe_path(name):
    p = PurePosixPath(name)
    if not name or '\\' in name or ':' in name or p.is_absolute() or '..' in p.parts or str(p) != name:
        raise ValueError('Unsafe package path: ' + name)
    return p


def pack(path, members):
    """Explicit modes and timestamps keep release packages independent of the build host."""
    seen = set()
    with build.gz_header_free(str(path)) as gz, tarfile.open(fileobj=gz, mode='w') as tf:
        for name, data, mode in sorted(members):
            safe_path(name)
            if name in seen:
                raise ValueError('Duplicate package path: ' + name)
            seen.add(name)
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), mode
            tf.addfile(info, io.BytesIO(data))


def artifact(path, base_url):
    data = path.read_bytes()
    return {'name': path.name, 'size': len(data), 'sha256': digest(data),
            'url': base_url + '/' + quote(path.name, safe='')}


def pack_app(folder, spec, version, out, base_url):
    src = Path(build.DIST) / folder / 'source'
    members = []
    # All identities in the package use the same Store-compatible version.
    for path in sorted(src.rglob('*')):
        if not path.is_file():
            continue
        rel = path.relative_to(src).as_posix()
        if rel.startswith('node_modules/oap-sdk/'):
            raise ValueError('Release must not contain BMW SDK: ' + rel)
        data = path.read_bytes()
        if rel in ('package.json', 'npm-shrinkwrap.json', 'javascript/kit/app-spec.json',
                   'rhmiData/application.json', 'rhmiDataStartWidget/application.json'):
            doc = json.loads(data)
            (doc['application'] if 'application' in doc else doc)['version'] = version
            if rel == 'package.json':
                doc['mguHmi'] = spec['mguHmi']
            data = json_bytes(doc)
        members.append(('package/' + rel, data, 0o644))
    pkg = src / 'package.json'
    if not pkg.is_file() or not (src / 'rhmiData/application.json').is_file():
        raise ValueError('Not an installable app: ' + folder)
    path = out / (spec['id'] + '@' + version + '.tgz')
    pack(path, members)
    entry = {k: copy.deepcopy(spec[k]) for k in META if k in spec}
    entry.update(id=spec['id'], kind='app', version=version, uninstallable=True,
                 artifact=artifact(path, base_url))
    require_deploy(entry, '0.3.2')
    app = read_json(src / 'rhmiData/application.json')['application']
    icon = src / 'rhmiData/variantGroups/Common/images' / app['entryPoints'][0]['icon']
    data = icon.read_bytes()
    if not data.startswith(b'\x89PNG\r\n\x1a\n') or len(data) > 128 * 1024:
        raise ValueError('Store icon must be a PNG of at most 128 KiB: ' + str(icon))
    name = spec['id'] + '.png'
    (out / name).write_bytes(data)
    entry.update(icon=base_url + '/' + quote(name), iconSha256=digest(data))
    return entry


def pack_tool(definition, out, base_url):
    folder = ROOT.joinpath(*safe_path(definition['path']).parts)
    manifest = read_json(folder / 'component.json')
    if manifest.get('kind') != 'tool' or manifest.get('build'):
        raise ValueError('Expected a prebuilt tool: ' + str(folder))
    members = []

    def take(source, target, mode=0o644):
        p = folder.joinpath(*safe_path(source).parts)
        data = p.read_bytes()
        members.append((target, data, mode))
        return data

    for f in manifest.get('files', []):
        safe_path(f['path'])
        if not re.fullmatch(r'[0-7]{3,4}', f['mode']):
            raise ValueError('Invalid file mode: ' + f['mode'])
        data = take(f.pop('from', f['path']), 'files/' + f['path'], int(f['mode'], 8))
        f['sha256'] = digest(data)
    for u in manifest.get('units', []):
        take(u.pop('from', u['name']), 'units/' + u['name'])
    for d in manifest.get('dropins', []):
        take(d.pop('from', d['name']), 'dropins/' + d['unit'] + '.d/' + d['name'])
    # This renderer also satisfies the controller gate's capability dependency.
    if manifest['id'] == 'idrive7-renderd':
        manifest['mguProvides'] = 'renderer'
    members.append(('component.json', json_bytes(manifest), 0o644))
    path = out / (manifest['id'] + '@' + manifest['version'] + '.tgz')
    pack(path, members)
    entry = {k: copy.deepcopy(manifest[k]) for k in META if k in manifest}
    entry.update({k: copy.deepcopy(definition[k]) for k in META if k in definition})
    if 'needs' in definition:
        entry['mguNeeds'] = definition['needs']
    entry.update(id=manifest['id'], kind='tool', version=manifest['version'], artifact=artifact(path, base_url))
    require_deploy(entry, '0.4.0' if entry.get('mguProvides') else '0.3.2')
    return entry


def require_deploy(entry, minimum):
    requires = entry.setdefault('requires', {})
    have = requires.get('mgu-deploy', '0.0.0')
    requires['mgu-deploy'] = max((have, minimum), key=lambda v: tuple(map(int, v.split('.'))))


def validate_catalog(catalog):
    entries = {}
    for c in catalog['components']:
        if not ID.fullmatch(c['id']) or not SEMVER.fullmatch(c['version']) or c['id'] in entries:
            raise ValueError('Invalid or duplicate component: ' + c['id'])
        if c.get('build'):
            raise ValueError('Release catalog must use prebuilt packages')
        entries[c['id']] = c
    for c in entries.values():
        for dep, minimum in c.get('requires', {}).items():
            if not SEMVER.fullmatch(minimum):
                raise ValueError('Invalid requirement: ' + dep)
            if dep == 'mgu-deploy':
                continue
            have = entries.get(dep)
            if not have or tuple(map(int, have['version'].split('.'))) < tuple(map(int, minimum.split('.'))):
                raise ValueError(c['id'] + ': missing requirement ' + dep + '>=' + minimum)
        for need in c.get('mguNeeds', []):
            if not any(e.get('mguProvides') == need for e in entries.values()):
                raise ValueError(c['id'] + ': missing capability ' + need)


def build_release(version, repository, language, out):
    if not SEMVER.fullmatch(version):
        raise ValueError('Version must be x.y.z')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise ValueError('Repository must be owner/repo')
    if prebuilt_hash() != source_hash():
        raise ValueError('Renderer prebuilt does not match its source; rebuild and commit it before publishing')
    out.mkdir(parents=True, exist_ok=True)
    if any(out.iterdir()):
        raise ValueError('Release output directory must be empty: ' + str(out))
    # The workflow binds these URLs to the actual release assets before publishing the catalog.
    base_url = 'https://github.com/' + repository + '/releases/download/cn-' + version
    components = []
    for path in sorted((ROOT / 'apps').glob('*/app.json')):
        spec = localize_spec(read_json(path), language)
        spec.setdefault('id', path.parent.name)
        spec.setdefault('mguHmi', ['ID7', 'ID8'])
        build.build(path.parent.name, language, without_sdk=True)
        components.append(pack_app(path.parent.name, spec, version, out, base_url))
    for definition in read_json(ROOT / 'mgu-repo.json')['tools']:
        components.append(pack_tool(definition, out, base_url))
    catalog = {'schema': 2, 'components': components}
    validate_catalog(catalog)
    (out / 'catalog.json').write_bytes(json_bytes(catalog))
    print('Store release %s: %d components in %s' % (version, len(components), out))


def bind_release(out, release, private=False):
    """Public download URLs work without a token; private assets need the authenticated API."""
    assets = {a['name']: a for a in release['assets']}
    catalog = read_json(out / 'catalog.json')
    for c in catalog['components']:
        a = c['artifact']
        remote = assets[a['name']]
        if remote['size'] != a['size']:
            raise ValueError('Uploaded asset has the wrong size: ' + a['name'])
        a['url'] = remote['url' if private else 'browser_download_url']
        if c.get('icon'):
            c['icon'] = assets[c['id'] + '.png']['url' if private else 'browser_download_url']
    validate_catalog(catalog)
    (out / 'catalog.json').write_bytes(json_bytes(catalog))


def check_update(out, previous):
    catalog = read_json(out / 'catalog.json')
    validate_catalog(catalog)
    old = {c['id']: c for c in previous['components']}
    for c in catalog['components']:
        before = old.get(c['id'])
        if not before:
            continue
        if tuple(map(int, c['version'].split('.'))) < tuple(map(int, before['version'].split('.'))):
            raise ValueError('Refusing to publish an older version of ' + c['id'])
        if c['version'] == before['version'] and c['artifact']['sha256'] != before['artifact']['sha256']:
            raise ValueError('Package changed without a version bump: ' + c['id'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version')
    parser.add_argument('--repository')
    parser.add_argument('--language', choices=languages(), default='zh-CN')
    parser.add_argument('--out', type=Path, default=ROOT / 'dist/store-release')
    parser.add_argument('--bind-release', type=Path, help='bind an existing catalog to a GitHub release API response')
    parser.add_argument('--check-update', type=Path, help='refuse downgrades and changed packages with unchanged versions')
    parser.add_argument('--private', action='store_true')
    args = parser.parse_args()
    if args.check_update:
        check_update(args.out, read_json(args.check_update))
    elif args.bind_release:
        bind_release(args.out, read_json(args.bind_release), args.private)
    elif args.version and args.repository:
        build_release(args.version, args.repository, args.language, args.out)
    else:
        parser.error('--version and --repository are required to build')


if __name__ == '__main__':
    main()
